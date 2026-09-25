"""Bug intake: open GitHub issues joined with the Planner cards that already track them.

Issues are the inbox; Planner is the commitment. Nothing here creates cards. Claude proposes,
Om confirms, and only then does create_task(issue=...) write one.
"""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor

from . import conventions as conv
from . import github as gh
from . import links
from .auth import AuthError
from .board import Board
from .config import Config, ConfigError
from .graph import GraphError

FIELD_CAP = 600  # per form field in intake_list; intake_item returns them whole
BODY_CAP = 1500  # body of an issue not filed through the form, in intake_list
COMMENT_CAP = 1000
MAX_COMMENTS = 5
_SEVERITY_RANK = {"S1": 1, "S2": 2, "S3": 3}
CLOSE_REASONS = ("completed", "not planned")


class Intake:
    def __init__(
        self,
        cfg: Config,
        board: Board,
        client: gh.GitHubClient | None = None,
        clock: Callable[[], dt.datetime] | None = None,
    ):
        if cfg.github is None:
            raise ConfigError(
                "No `github:` section in the config, so bug intake is off. "
                "See config.example.yaml."
            )
        self.cfg = cfg
        self.gcfg = cfg.github
        self.board = board
        self.gh = client or gh.GitHubClient(cfg.github)
        self._now = clock or (lambda: dt.datetime.now(dt.UTC))

    # ---- helpers -------------------------------------------------------------

    def _repos(self, project: str | None) -> list[str]:
        repos = self.gcfg.repos
        if not project:
            return list(repos.values())
        p = project.strip().strip("[]")
        if p.upper() in repos:
            return [repos[p.upper()]]
        for r in repos.values():
            if p.lower() in (r.lower(), r.split("/", 1)[1].lower()):
                return [r]
        raise conv.ConventionError(
            f"Unknown project {project!r}. Configured: "
            + ", ".join(f"{k} ({v})" for k, v in repos.items())
        )

    def _key(self, raw: dict, repo: str) -> str:
        # Key off gh's URL: it follows repo renames, and create_task stores the same URL.
        found = links.issue_in_url(raw.get("url") or "") or (repo, int(raw["number"]))
        return links.link_key(*found, self.gcfg.renamed)

    def _summary(self, raw: dict, repo: str, cap: int | None) -> dict:
        labels = [lbl.get("name", "") for lbl in raw.get("labels") or []]
        body = raw.get("body") or ""
        fields = gh.parse_form(body)
        created = raw.get("createdAt")
        age = (self._now() - dt.datetime.fromisoformat(created)).days if created else None
        out = {
            "issue": f"{repo}#{raw['number']}",
            "repo": repo,
            "number": raw["number"],
            "url": raw.get("url"),
            "title": raw.get("title"),
            "labels": labels,
            "type": raw.get("type"),
            "issue_fields": raw.get("issue_fields") or {},  # org fields: Priority, Product, Area
            "severity": gh.severity(labels, fields),
            "age_days": age,
            "author": (raw.get("author") or {}).get("login"),
            "suggested_prefix": self.gcfg.prefix_for(repo),
            "linked_task_id": None,
            "fields": {k: _cap(v, cap) for k, v in fields.items()},
        }
        if not fields and cap:
            out["body"] = _cap(body.strip(), BODY_CAP)  # free-form issue: the body is the repro
        return out

    def _lookup(self, issue: str) -> tuple[links.IssueRef, dict]:
        """Parse an issue reference and fetch the issue. PRs are refused."""
        try:
            ref = links.parse_ref(issue, self.gcfg.repos)
        except links.BareIssueNumber as bare:
            return self._find_number(bare.number)
        raw = self.gh.view_issue(ref.repo, ref.number)
        if raw.get("is_pr"):
            raise conv.ConventionError(f"{ref} is a pull request, not an issue.")
        return ref, raw

    def _find_number(self, number: int) -> tuple[links.IssueRef, dict]:
        """"#36" with no repo: look in every configured repo. Numbers restart per repo, so it is
        used only when one repo has that issue, or exactly one of several is open."""
        repos = list(self.gcfg.repos.values())
        with ThreadPoolExecutor(max_workers=len(repos)) as pool:
            futures = [(r, pool.submit(self.gh.view_issue, r, number)) for r in repos]
        found: list[tuple[links.IssueRef, dict]] = []
        for r, f in futures:
            try:
                raw = f.result()
            except gh.GitHubError as e:
                if e.not_found:
                    continue
                raise  # a repo we couldn't check might also have #N; don't guess
            if not raw.get("is_pr"):
                found.append((links.IssueRef(r, number), raw))
        if not found:
            raise conv.ConventionError(f"No issue #{number} in {', '.join(repos)}.")
        if len(found) > 1:
            open_ = [x for x in found if (x[1].get("state") or "").upper() == "OPEN"]
            if len(open_) == 1:
                return open_[0]
            listing = "; ".join(
                f"{ref} {raw.get('title')!r} ({(raw.get('state') or '').lower()})"
                for ref, raw in found)
            raise conv.ConventionError(
                f"#{number} is in more than one repo: {listing}. Say which, e.g. {found[0][0]}.")
        return found[0]

    def _priority_rank(self, repos: list[str]) -> dict[str, int]:
        """Option order of the org's Priority field (Urgent first). {} if it can't be read:
        a missing catalogue should never stop the listing."""
        rank: dict[str, int] = {}
        for org in sorted({r.split("/", 1)[0] for r in repos}):
            try:
                catalogue = self.gh.field_catalogue(org)
            except gh.GitHubError:
                continue
            for f in catalogue:
                if f["name"].lower() == "priority":
                    for i, opt in enumerate(f["options"]):
                        rank.setdefault(opt["name"].lower(), i)
        return rank

    # ---- tools ---------------------------------------------------------------

    def open_issues(self, project: str | None = None, include_linked: bool = False) -> dict:
        repos = self._repos(project)
        # One call per (repo, issue type), merged below.
        jobs = [(r, t) for r in repos for t in self.gcfg.intake_types]
        with ThreadPoolExecutor(max_workers=min(8, len(jobs))) as pool:
            futures = [(r, t, pool.submit(self.gh.list_issues, r, t)) for r, t in jobs]
            results = []
            for r, t, f in futures:
                try:
                    results.append((r, t, f.result(), None))
                except gh.GitHubError as e:
                    results.append((r, t, None, e))

        errors = [e for *_, e in results if e]
        if errors and len(errors) == len(results):
            raise errors[0]  # nothing came back: gh is missing, logged out, or blocked
        repo_errors: dict[str, str] = {}
        truncated: list[str] = []
        raw_by_key: dict[str, tuple[str, dict]] = {}
        for r, t, res, err in results:
            if err:
                repo_errors.setdefault(r, str(err))
                continue
            issues, total = res
            if total > len(issues):
                truncated.append(f"{r}:{t}")
            for raw in issues:
                raw_by_key.setdefault(self._key(raw, r), (r, raw))

        linked = self.board.issue_links()
        shown: list[dict] = []
        linked_done: list[dict] = []
        hidden = 0
        for key, (r, raw) in raw_by_key.items():
            item = self._summary(raw, r, FIELD_CAP)
            cards = linked.get(key, [])
            if cards:
                open_cards = [c for c in cards if not c["completed"]]
                item["linked_task_id"] = (open_cards or cards)[0]["task_id"]
                if not include_linked:
                    if open_cards:
                        hidden += 1
                    else:
                        linked_done.append({k: item[k] for k in (
                            "issue", "title", "url", "age_days", "linked_task_id")})
                    continue
            shown.append(item)

        prio = self._priority_rank(repos)
        shown.sort(key=lambda i: (
            prio.get(str(i["issue_fields"].get("Priority") or "").lower(), 99),
            _SEVERITY_RANK.get(i["severity"], 9),
            -(i["age_days"] or 0), i["repo"], i["number"]))
        out = {
            "as_of": self._now().isoformat(timespec="seconds"),
            "types": list(self.gcfg.intake_types),
            "repos": repos,
            "issues": shown,
            "linked_done": linked_done,
            "hidden_linked": hidden,
        }
        if repo_errors:
            out["repo_errors"] = [{"repo": r, "message": m} for r, m in repo_errors.items()]
        if truncated:
            out["truncated"] = truncated
        return out

    def detail(self, issue: str) -> dict:
        ref, raw = self._lookup(issue)
        out = self._summary(raw, ref.repo, None)
        out["state"] = (raw.get("state") or "").lower()
        out["assignees"] = [a.get("login") for a in raw.get("assignees") or []]
        out["body"] = raw.get("body") or ""
        out["comments"] = [
            {
                "author": (c.get("author") or {}).get("login"),
                "created": (c.get("createdAt") or "")[:10] or None,
                "body": _cap(c.get("body") or "", COMMENT_CAP),
            }
            for c in (raw.get("comments") or [])[-MAX_COMMENTS:]
        ]
        cards = self.board.issue_links().get(self._key(raw, ref.repo), [])
        out["linked_task_id"] = cards[0]["task_id"] if cards else None
        out["linked_task_completed"] = cards[0]["completed"] if cards else None
        return out

    # ---- writes to GitHub: only when Om asks ------------------------------------

    def close(self, issue: str, reason: str = "completed", comment: str | None = None) -> dict:
        """Close the issue on GitHub (unless it already is), then tick every open card that
        tracks it. Ticking a card never closes an issue; this is the only path that does."""
        why = (reason or "completed").strip().lower().replace("_", " ")
        if why not in CLOSE_REASONS:
            raise conv.ConventionError(f"reason must be one of: {', '.join(CLOSE_REASONS)}")
        ref, raw = self._lookup(issue)
        already = (raw.get("state") or "").upper() != "OPEN"
        if not already:
            self.gh.close_issue(ref.repo, ref.number, why, comment)
        out: dict = {
            "issue": str(ref),
            "url": raw.get("url"),
            "title": raw.get("title"),
            "closed": True,
            "already_closed": already,
            "reason": None if already else why,
            "cards_ticked": [],
        }
        # GitHub is already written; a Planner failure must not hide that, so report it here.
        try:
            for c in self.board.issue_links().get(self._key(raw, ref.repo), []):
                if not c["completed"]:
                    card = self.board.update(c["task_id"], percent_complete=100)
                    out["cards_ticked"].append(
                        {"id": card["id"], "title": card["title"], "bucket": card["bucket"]})
        except (GraphError, AuthError) as e:
            out["card_error"] = f"Issue closed, but the card could not be ticked: {e}"
        return out

    def set_fields(self, issue: str, fields: dict[str, str]) -> dict:
        """Set org issue fields, e.g. {"Priority": "High", "Area": "API"}. Names and options
        match case-insensitively; dates are YYYY-MM-DD; "" clears a field."""
        if not fields:
            raise conv.ConventionError('Nothing to set. Pass e.g. {"Priority": "High"}.')
        ref, raw = self._lookup(issue)
        org = ref.repo.split("/", 1)[0]
        try:
            values = self._field_values(org, fields)
        except conv.ConventionError:  # a field or option added since the catalogue was cached
            values = self._field_values(org, fields, refresh=True)
        self.gh.set_issue_fields(raw["id"], values, ref.repo, ref.number)
        after = self.gh.view_issue(ref.repo, ref.number)
        return {
            "issue": str(ref),
            "url": after.get("url"),
            "title": after.get("title"),
            "type": after.get("type"),
            "issue_fields": after.get("issue_fields") or {},
        }

    def _field_values(self, org: str, fields: dict[str, str], refresh: bool = False) -> list[dict]:
        catalogue = self.gh.field_catalogue(org, refresh=refresh)
        by_name = {f["name"].lower(): f for f in catalogue}
        values = []
        for name, value in fields.items():
            f = by_name.get(str(name).strip().lower())
            if f is None:
                raise conv.ConventionError(
                    f"No issue field {name!r} in {org}. Fields: {', '.join(x['name'] for x in catalogue)}")
            v = str(value or "").strip()
            if not v:
                values.append({"fieldId": f["id"], "delete": True})
            elif f["kind"] == "single_select":
                opt = next((o for o in f["options"] if o["name"].lower() == v.lower()), None)
                if opt is None:
                    raise conv.ConventionError(
                        f"{f['name']} has no option {value!r}. Options: "
                        + ", ".join(o["name"] for o in f["options"]))
                values.append({"fieldId": f["id"], "singleSelectOptionId": opt["id"]})
            else:
                if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", v):
                    raise conv.ConventionError(f"{f['name']} needs a date as YYYY-MM-DD, got {value!r}.")
                values.append({"fieldId": f["id"], "dateValue": v})
        return values

    def resolve(self, issue: str, require_open: bool = True) -> links.IssueRef:
        """Parse and check an issue against GitHub before anything is written to the board."""
        ref, raw = self._lookup(issue)
        url = raw.get("url") or ""
        if require_open and (raw.get("state") or "").upper() != "OPEN":
            raise conv.ConventionError(
                f"{ref} is {str(raw.get('state')).lower()}; only open issues become new cards. "
                "Use link_issue to attach a closed one to an existing card."
            )
        return links.IssueRef(ref.repo, ref.number, url or None)


def _cap(text: str | None, limit: int | None) -> str | None:
    if text is None or limit is None or len(text) <= limit:
        return text
    return text[:limit].rstrip() + "…"
