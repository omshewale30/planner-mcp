"""An in-memory GitHub behind the gh runner interface: (args, timeout) -> (code, out, err).
Answers the GraphQL operations the server sends through `gh api graphql` (dispatching on the
operation name) with GitHub-shaped JSON, and uses gh's real stderr for the failures that
matter (logged out, SAML/SSO, unknown repo, unknown issue). Like GitHub, looking up a PR
number through issueOrPullRequest returns the PR, not an error."""

from __future__ import annotations

import json
import re
import subprocess
from itertools import pairwise

ORG_FIELDS = [
    {"__typename": "IssueFieldSingleSelect", "id": "F_pri", "name": "Priority", "options": [
        {"id": "O_urgent", "name": "Urgent"}, {"id": "O_high", "name": "High"},
        {"id": "O_medium", "name": "Medium"}, {"id": "O_low", "name": "Low"}]},
    {"__typename": "IssueFieldSingleSelect", "id": "F_product", "name": "Product", "options": [
        {"id": "O_piq", "name": "PIQ"}, {"id": "O_nimbus", "name": "Nimbus"},
        {"id": "O_quad", "name": "Quad"}]},
    {"__typename": "IssueFieldSingleSelect", "id": "F_area", "name": "Area", "options": [
        {"id": "O_fe", "name": "Frontend"}, {"id": "O_api", "name": "API"},
        {"id": "O_ai", "name": "AI / LLM"}]},
    {"__typename": "IssueFieldDate", "id": "F_target", "name": "Target date"},
]


def _opt(args: list[str], name: str) -> str | None:
    return args[args.index(name) + 1] if name in args else None


def _vars(args: list[str]) -> tuple[str, dict]:
    query, out = "", {}
    for flag, kv in pairwise(args):
        if flag not in ("-f", "-F"):
            continue
        k, _, v = kv.partition("=")
        if k == "query":
            query = v
        elif k.endswith("[]"):
            out.setdefault(k[:-2], []).append(v)
        else:
            out[k] = int(v) if flag == "-F" and v.isdigit() else v
    return query, out


class FakeGh:
    def __init__(self):
        self.repos: dict[str, dict[int, dict]] = {}
        self.prs: dict[str, dict[int, dict]] = {}
        self.url_name: dict[str, str] = {}  # repo -> name GitHub puts in URLs (after a rename)
        self.org_fields = json.loads(json.dumps(ORG_FIELDS))
        self.calls: list[list[str]] = []
        self.mode: str | None = None  # "missing" | "timeout" | "logged_out" | "sso"
        self.broken: set[str] = set()  # repos that answer "Could not resolve"
        self.fields_broken = False  # org field catalogue unreadable
        self.path = "/fake/bin/gh"

    def add_repo(self, repo: str) -> None:
        self.repos.setdefault(repo, {})
        self.prs.setdefault(repo, {})

    def add_issue(self, repo, number, title, labels=("bug",), body="", state="OPEN",
                  author="kaijifu", created="2026-09-20T12:00:00Z", comments=(),
                  type="Bug", fields=None):
        self.add_repo(repo)
        self.repos[repo][number] = {
            "id": f"I_{repo}_{number}", "number": number, "title": title, "body": body,
            "state": state, "labels": list(labels), "author": author, "createdAt": created,
            "type": type, "fields": dict(fields or {}), "assignees": [],
            "comments": [{"author": {"login": a}, "createdAt": created, "body": b}
                         for a, b in comments],
        }

    def add_pr(self, repo, number, title, state="MERGED"):
        self.add_repo(repo)
        self.prs[repo][number] = {"number": number, "title": title, "state": state}

    def issue(self, repo: str, number: int) -> dict:
        return self.repos[repo][number]

    def graphql_calls(self, operation: str) -> list[dict]:
        out = []
        for args in self.calls:
            if args[:2] == ["api", "graphql"]:
                query, variables = _vars(args)
                if re.search(rf"\b(query|mutation)\s+{operation}\b", query):
                    out.append(variables)
        return out

    # ---- transport ---------------------------------------------------------------

    def __call__(self, args: list[str], timeout: float) -> tuple[int, str, str]:
        self.calls.append(list(args))
        if self.mode == "missing":
            raise FileNotFoundError("gh")
        if self.mode == "timeout":
            raise subprocess.TimeoutExpired(["gh", *args], timeout)
        if self.mode == "logged_out":
            return 1, "", ("You are not logged into any GitHub hosts. To log in, run: "
                           "gh auth login\n")
        if self.mode == "sso":
            return 1, "", ("GraphQL: Resource protected by organization SAML enforcement. You "
                           "must grant your OAuth token access to this organization. (repository)\n")
        if args[:2] == ["auth", "status"]:
            return 0, "", "github.com\n  ✓ Logged in to github.com account om (keyring)\n"
        if args[:2] == ["issue", "close"]:
            return self._close(args)
        if args[:2] == ["api", "graphql"]:
            query, v = _vars(args)
            op = re.search(r"\b(?:query|mutation)\s+(\w+)", query)
            handler = getattr(self, f"_op_{op.group(1)}", None) if op else None
            if handler is None:
                return 1, "", f"unknown GraphQL operation in {query[:60]!r}\n"
            return handler(query, v)
        return 1, "", f"unknown command: {args}\n"

    def _repo_or_error(self, v: dict) -> tuple[str | None, tuple | None]:
        repo = f"{v.get('owner')}/{v.get('name')}"
        if repo not in self.repos or repo in self.broken:
            msg = f"GraphQL: Could not resolve to a Repository with the name '{repo}'. (repository)\n"
            return None, (1, "", msg)
        return repo, None

    def _node(self, repo: str, i: dict, full: bool = False) -> dict:
        node = {
            "number": i["number"], "title": i["title"], "body": i["body"], "state": i["state"],
            "url": f"https://github.com/{self.url_name.get(repo, repo)}/issues/{i['number']}",
            "createdAt": i["createdAt"], "author": {"login": i["author"]},
            "labels": {"nodes": [{"name": n} for n in i["labels"]]},
            "issueType": {"name": i["type"]} if i["type"] else None,
            "issueFieldValues": {"nodes": [self._value(k, val) for k, val in i["fields"].items()]},
        }
        if full:
            node.update({
                "__typename": "Issue", "id": i["id"],
                "assignees": {"nodes": [{"login": a} for a in i["assignees"]]},
                "comments": {"nodes": i["comments"][-5:]},
            })
        return node

    def _value(self, field: str, value: str) -> dict:
        f = next(x for x in self.org_fields if x["name"] == field)
        if f["__typename"] == "IssueFieldDate":
            return {"__typename": "IssueFieldDateValue", "value": value, "field": {"name": field}}
        return {"__typename": "IssueFieldSingleSelectValue", "name": value, "field": {"name": field}}

    def _op_IntakeList(self, query, v):
        repo, err = self._repo_or_error(v)
        if err:
            return err
        hits = [
            i for _, i in sorted(self.repos[repo].items(), key=lambda kv: kv[1]["createdAt"])
            if i["state"] == "OPEN" and i["type"] == v["type"]
        ]
        page = hits[: int(v["first"])]
        data = {"repository": {"issues": {"totalCount": len(hits),
                                          "nodes": [self._node(repo, i) for i in page]}}}
        return 0, json.dumps({"data": data}), ""

    def _op_IntakeIssue(self, query, v):
        repo, err = self._repo_or_error(v)
        if err:
            return err
        number = int(v["number"])
        if number in self.repos[repo]:
            node = self._node(repo, self.repos[repo][number], full=True)
        elif number in self.prs[repo]:
            pr = self.prs[repo][number]
            node = {"__typename": "PullRequest", "state": pr["state"],
                    "url": f"https://github.com/{repo}/pull/{number}"}
        else:
            return 1, "", (f"GraphQL: Could not resolve to an issue or pull request with the "
                           f"number of {number}. (repository.issueOrPullRequest)\n")
        return 0, json.dumps({"data": {"repository": {"issueOrPullRequest": node}}}), ""

    def _close(self, args):
        repo, number = _opt(args, "--repo"), int(args[2])
        if repo not in self.repos:
            msg = f"GraphQL: Could not resolve to a Repository with the name '{repo}'. (repository)\n"
            return 1, "", msg
        issue = self.repos[repo].get(number)
        if issue is None:
            return 1, "", (f"GraphQL: Could not resolve to an issue or pull request with the "
                           f"number of {number}. (repository.issue)\n")
        if issue["state"] == "CLOSED":  # gh warns and still exits 0
            return 0, "", f"! Issue {repo}#{number} ({issue['title']}) is already closed\n"
        issue["state"] = "CLOSED"
        issue["closed_reason"] = _opt(args, "--reason")
        if comment := _opt(args, "--comment"):
            issue["comments"].append({"author": {"login": "omshewale30"},
                                      "createdAt": "2026-09-25T12:00:00Z", "body": comment})
        return 0, "", f"✓ Closed issue {repo}#{number} ({issue['title']})\n"

    def _op_SetIssueFields(self, query, v):
        literal = re.search(r"input:\s*(\{.*\})\s*\)", query, re.DOTALL).group(1)
        data = json.loads(re.sub(r"([{,]\s*)(\w+):", r'\1"\2":', literal))
        issue = next((i for issues in self.repos.values() for i in issues.values()
                      if i["id"] == data["issueId"]), None)
        if issue is None:
            return 1, "", "GraphQL: Could not resolve to a node with the global id.\n"
        for val in data["issueFields"]:
            f = next(x for x in self.org_fields if x["id"] == val["fieldId"])
            if val.get("delete"):
                issue["fields"].pop(f["name"], None)
            elif "singleSelectOptionId" in val:
                opt = next(o for o in f["options"] if o["id"] == val["singleSelectOptionId"])
                issue["fields"][f["name"]] = opt["name"]
            else:
                issue["fields"][f["name"]] = val["dateValue"]
        return 0, json.dumps({"data": {"setIssueFieldValue": {"clientMutationId": None}}}), ""

    def _op_OrgFields(self, query, v):
        if self.fields_broken:
            return 1, "", "GraphQL: Field 'issueFields' doesn't exist on type 'Organization'\n"
        return 0, json.dumps({"data": {"organization": {"issueFields": {
            "nodes": self.org_fields}}}}), ""
