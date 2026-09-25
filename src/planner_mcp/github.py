"""GitHub Issues through the `gh` CLI, run as Om under his existing `gh` login.

Reads open issues with their org issue type and fields. Writes only when Om asks: closing an
issue, and setting its org issue fields (Priority, Product, Area, ...).

No GitHub token lives in this server. Claude desktop starts us without the shell's PATH, so the
binary is found explicitly (config `gh_path`, then PATH, then the Homebrew locations).

`gh` must never touch our stdin/stdout: those are the MCP transport. Every call runs with
stdin=/dev/null and captured output, with prompts, pagers and update checks disabled.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from collections.abc import Callable
from typing import Any

from .config import GitHubConfig

# args, timeout -> (exit code, stdout, stderr). May raise FileNotFoundError (no gh binary)
# or subprocess.TimeoutExpired. Tests pass a fake with the same shape.
Runner = Callable[[list[str], float], tuple[int, str, str]]

FALLBACK_PATHS = ("/opt/homebrew/bin/gh", "/usr/local/bin/gh")
GH_ENV = {
    "GH_PROMPT_DISABLED": "1",
    "GH_NO_UPDATE_NOTIFIER": "1",
    "GH_PAGER": "cat",
    "GH_SPINNER_DISABLED": "1",
    "NO_COLOR": "1",
}
# Issues are read over GraphQL (`gh api graphql`) so one call returns the org issue type and
# issue fields (Priority, Product, Area, ...) alongside the body. Operation names matter:
# tests/fake_gh.py dispatches on them.
_ISSUE_PARTS = """
fragment IssueParts on Issue {
  number title body url createdAt state
  author { login }
  labels(first: 20) { nodes { name } }
  issueType { name }
  issueFieldValues(first: 20) {
    nodes {
      __typename
      ... on IssueFieldSingleSelectValue { name field { ... on IssueFieldSingleSelect { name } } }
      ... on IssueFieldDateValue { value field { ... on IssueFieldDate { name } } }
    }
  }
}"""
LIST_QUERY = """
query IntakeList($owner: String!, $name: String!, $first: Int!, $type: String!) {
  repository(owner: $owner, name: $name) {
    issues(states: OPEN, filterBy: {type: $type}, first: $first,
           orderBy: {field: CREATED_AT, direction: ASC}) {
      totalCount
      nodes { ...IssueParts }
    }
  }
}""" + _ISSUE_PARTS
VIEW_QUERY = """
query IntakeIssue($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    issueOrPullRequest(number: $number) {
      __typename
      ... on PullRequest { url state }
      ... on Issue {
        id ...IssueParts
        assignees(first: 10) { nodes { login } }
        comments(last: 5) { nodes { author { login } createdAt body } }
      }
    }
  }
}""" + _ISSUE_PARTS
FIELDS_QUERY = """
query OrgFields($org: String!) {
  organization(login: $org) {
    issueFields(first: 50) {
      nodes {
        __typename
        ... on IssueFieldSingleSelect { id name options { id name } }
        ... on IssueFieldDate { id name }
      }
    }
  }
}"""


class GitHubError(RuntimeError):
    def __init__(self, message: str, repo: str | None = None, not_found: bool = False):
        super().__init__(message)
        self.repo = repo
        self.not_found = not_found  # the repo answered: no issue or PR with that number


def find_gh(configured: str | None = None) -> str | None:
    if configured:
        p = os.path.expanduser(configured)
        return p if os.path.exists(p) else None
    found = shutil.which("gh")
    if found:
        return found
    return next((p for p in FALLBACK_PATHS if os.path.exists(p)), None)


class SubprocessRunner:
    def __init__(self, gh_path: str | None = None):
        self.path = find_gh(gh_path)

    def __call__(self, args: list[str], timeout: float) -> tuple[int, str, str]:
        if not self.path:
            raise FileNotFoundError("gh")
        r = subprocess.run(
            [self.path, *args],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            check=False,
            text=True,
            timeout=timeout,
            env={**os.environ, **GH_ENV},
        )
        return r.returncode, r.stdout, r.stderr


class GitHubClient:
    def __init__(self, cfg: GitHubConfig, runner: Runner | None = None):
        self.cfg = cfg
        self._runner = runner or SubprocessRunner(cfg.gh_path)
        self._catalogue: dict[str, list[dict]] = {}  # org -> issue field definitions

    @property
    def gh_path(self) -> str | None:
        return getattr(self._runner, "path", None)

    def _gh(self, args: list[str], repo: str | None = None, number: int | None = None) -> str:
        try:
            code, out, err = self._runner(args, self.cfg.timeout_s)
        except FileNotFoundError:
            where = self.cfg.gh_path or "PATH, " + ", ".join(FALLBACK_PATHS)
            raise GitHubError(
                f"gh CLI not found (looked in {where}). Install it or set github.gh_path in the config."
            ) from None
        except subprocess.TimeoutExpired:
            raise GitHubError(
                f"gh timed out after {self.cfg.timeout_s:g}s" + (f" on {repo}" if repo else ""), repo
            ) from None
        if code != 0:
            raise GitHubError(_explain(err, repo, number), repo,
                              not_found="Could not resolve to an issue" in (err or ""))
        return out

    def _json(self, args: list[str], repo: str | None = None, number: int | None = None) -> Any:
        out = self._gh(args, repo, number)
        try:
            return json.loads(out)
        except ValueError:
            raise GitHubError(f"Unexpected gh output: {out[:200]!r}", repo) from None

    def _graphql(self, query: str, strings: dict[str, str], ints: dict[str, int] | None = None,
                 repo: str | None = None, number: int | None = None) -> dict:
        args = ["api", "graphql", "-f", f"query={query}"]
        for k, v in strings.items():
            for item in v if isinstance(v, list) else [v]:
                args += ["-f", f"{k}[]={item}" if isinstance(v, list) else f"{k}={item}"]
        for k, n in (ints or {}).items():
            args += ["-F", f"{k}={int(n)}"]
        return self._json(args, repo, number).get("data") or {}

    def list_issues(self, repo: str, issue_type: str) -> tuple[list[dict], int]:
        """Open issues of org issue type `issue_type` (e.g. Bug), oldest first, plus the total
        that matched."""
        owner, name = repo.split("/", 1)
        data = self._graphql(LIST_QUERY, {"owner": owner, "name": name, "type": issue_type},
                             {"first": self.cfg.limit_per_repo}, repo)
        issues = (data.get("repository") or {}).get("issues") or {}
        return [_issue(n) for n in issues.get("nodes") or []], int(issues.get("totalCount") or 0)

    def view_issue(self, repo: str, number: int) -> dict:
        """One issue with its node id, assignees and last comments. A PR number comes back
        as {"is_pr": True, ...} rather than an error."""
        owner, name = repo.split("/", 1)
        data = self._graphql(VIEW_QUERY, {"owner": owner, "name": name}, {"number": number},
                             repo, number)
        node = (data.get("repository") or {}).get("issueOrPullRequest")
        if not node:
            raise GitHubError(f"{repo}#{number} not found.", repo, not_found=True)
        if node.get("__typename") == "PullRequest":
            return {"is_pr": True, "number": number, "url": node.get("url"), "state": node.get("state")}
        return _issue(node)

    def field_catalogue(self, org: str, refresh: bool = False) -> list[dict]:
        """The org's issue field definitions: [{id, name, kind, options: [{id, name}]}]."""
        if refresh or org not in self._catalogue:
            data = self._graphql(FIELDS_QUERY, {"org": org})
            nodes = ((data.get("organization") or {}).get("issueFields") or {}).get("nodes") or []
            self._catalogue[org] = [
                {
                    "id": n["id"],
                    "name": n["name"],
                    "kind": "single_select" if "options" in n else "date",
                    "options": n.get("options") or [],
                }
                for n in nodes
                if n.get("id") and n.get("name")
            ]
        return self._catalogue[org]

    # ---- writes: only when Om asks for them (see server INSTRUCTIONS) ----------------

    def close_issue(self, repo: str, number: int, reason: str, comment: str | None = None) -> None:
        """reason: "completed" or "not planned". comment is posted as the issue closes."""
        args = ["issue", "close", str(number), "--repo", repo, "--reason", reason]
        if comment:
            args += ["--comment", comment]
        self._gh(args, repo, number)

    def set_issue_fields(self, issue_id: str, values: list[dict], repo: str | None = None,
                         number: int | None = None) -> None:
        """values: [{"fieldId", "singleSelectOptionId" | "dateValue"}] or {"fieldId", "delete"}.
        GitHub upserts only the fields passed; others keep their values."""
        literal = _gql_literal({"issueId": issue_id, "issueFields": values})
        self._graphql(
            f"mutation SetIssueFields {{ setIssueFieldValue(input: {literal}) "
            "{ clientMutationId } }",
            {}, repo=repo, number=number,
        )

    def auth_problem(self) -> str | None:
        """None if `gh auth status` is happy, else a message saying what to do."""
        try:
            self._gh(["auth", "status"])
        except GitHubError as e:
            return str(e)
        return None


def _explain(stderr: str, repo: str | None, number: int | None) -> str:
    """Turn gh's stderr into something Claude can act on."""
    err = stderr or ""
    first = next((ln.strip() for ln in err.splitlines() if ln.strip()), "gh failed with no output")
    if re.search(r"gh auth login|not logged in|HTTP 401|Bad credentials", err, re.IGNORECASE):
        return "gh isn't signed in to GitHub. Run `gh auth login` in a terminal, then retry."
    if re.search(r"SAML|single sign-on|\bSSO\b|OAuth App access restrictions", err,
                 re.IGNORECASE):
        return (
            f"GitHub refused gh's token for {repo or 'this org'} (SSO / OAuth app restriction). "
            "Run `gh auth refresh -h github.com` and authorize the FO-AI org, or ask an org owner."
        )
    if "Could not resolve to a Repository" in err:
        return f"Repo {repo} not found or no access. Renamed? Update github.repos in the config."
    if "Could not resolve to an issue" in err:
        return f"{repo}#{number} not found."
    if re.search(r"TLS handshake timeout|dial tcp|no such host|i/o timeout|connection refused",
                 err, re.IGNORECASE):
        return "Can't reach GitHub right now (network). Check the connection and retry."
    if re.search(r"HTTP 403|rate limit", err, re.IGNORECASE):
        return f"GitHub refused the request: {first}"
    return f"gh failed: {first}"


def _gql_literal(value: Any) -> str:
    """A GraphQL input literal from plain Python (keys are our own identifiers). Only fed
    GitHub node/option IDs and validated dates; route free text through variables instead."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, dict):
        return "{" + ", ".join(f"{k}: {_gql_literal(v)}" for k, v in value.items()) + "}"
    if isinstance(value, list):
        return "[" + ", ".join(_gql_literal(v) for v in value) + "]"
    return json.dumps(str(value))


def _issue(node: dict) -> dict:
    """GraphQL issue node -> the flat shape the rest of the server uses."""
    fields: dict[str, str] = {}
    for v in (node.get("issueFieldValues") or {}).get("nodes") or []:
        name = (v.get("field") or {}).get("name")
        value = v.get("name") if "name" in v else v.get("value")
        if name and value is not None:
            fields[name] = value
    out = {
        "number": node.get("number"),
        "title": node.get("title"),
        "body": node.get("body") or "",
        "url": node.get("url"),
        "createdAt": node.get("createdAt"),
        "state": node.get("state"),
        "author": node.get("author") or {},
        "labels": (node.get("labels") or {}).get("nodes") or [],
        "type": (node.get("issueType") or {}).get("name"),
        "issue_fields": fields,
    }
    if "id" in node:
        out["id"] = node["id"]
        out["assignees"] = (node.get("assignees") or {}).get("nodes") or []
        out["comments"] = (node.get("comments") or {}).get("nodes") or []
    return out


# ---- issue bodies ------------------------------------------------------------

_HEADING = re.compile(r"^###[ \t]+(.+?)[ \t]*$", re.MULTILINE)
_SEV_LABEL = re.compile(r"^(?:(?:sev|severity)[\s:/-]*S?|S)([1-3])$", re.IGNORECASE)
_SEV_TEXT = re.compile(r"\bS([1-3])\b", re.IGNORECASE)
NO_RESPONSE = "_No response_"


def parse_form(body: str | None) -> dict[str, str | None]:
    """Issue-form bodies render as `### Label` sections. Keys are slugged labels
    ("Steps to reproduce" -> steps_to_reproduce). A body without sections gives {}."""
    parts = _HEADING.split((body or "").replace("\r\n", "\n"))
    if len(parts) < 3:
        return {}
    out: dict[str, str | None] = {}
    for head, value in zip(parts[1::2], parts[2::2]):
        key = re.sub(r"[^a-z0-9]+", "_", head.lower()).strip("_")
        v = value.strip()
        if fenced := re.fullmatch(r"```[\w-]*\n(.*?)\n?```", v, re.DOTALL):  # `render:` fields
            v = fenced.group(1).strip()
        out[key] = None if v in ("", NO_RESPONSE) else v
    return out


def severity(labels: list[str], fields: dict[str, str | None]) -> str | None:
    """A severity label (S1, severity:S2, ...) wins: it is the triager's call. Else the form."""
    for name in labels:
        if m := _SEV_LABEL.match(name.strip()):
            return f"S{m.group(1)}"
    if m := _SEV_TEXT.search(fields.get("severity") or ""):
        return f"S{m.group(1)}"
    return None
