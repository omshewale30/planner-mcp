"""Card <-> GitHub issue links. Pure functions, no I/O.

A card is linked to an issue by a Planner external reference (plannerTaskDetails.references)
whose URL is the issue URL. That is the source of truth for dedup. The `Issue: owner/repo#N`
first line in the notes is for people and is never read back.

Reference keys are URLs with `%` `.` `:` `@` `#` percent-encoded (OData open-type property
names cannot contain them); Graph returns them encoded the same way.
"""

from __future__ import annotations

import re
import urllib.parse
from dataclasses import dataclass

from .conventions import ConventionError

REFERENCE_TYPE = "#microsoft.graph.plannerExternalReference"
_KEY_ESCAPES = {"%": "%25", ".": "%2E", ":": "%3A", "@": "%40", "#": "%23"}
_ISSUE_URL_RE = re.compile(
    r"^https?://github\.com/([\w.-]+)/([\w.-]+)/issues/(\d+)/?$", re.IGNORECASE
)
_ANY_URL_RE = re.compile(
    r"^https?://github\.com/([\w.-]+)/([\w.-]+)/(issues|pull)/(\d+)/?$", re.IGNORECASE
)
_FULL_REF_RE = re.compile(r"^([\w.-]+)/([\w.-]+)#(\d+)$")
_SHORT_REF_RE = re.compile(r"^([\w.-]+)#(\d+)$")


@dataclass(frozen=True)
class IssueRef:
    repo: str  # configured spelling, e.g. "FO-AI/nimbus"
    number: int
    url: str | None = None  # canonical URL as gh reports it (follows repo renames)

    def __str__(self) -> str:
        return f"{self.repo}#{self.number}"

    @property
    def link_url(self) -> str:
        return self.url or f"https://github.com/{self.repo}/issues/{self.number}"


class BareIssueNumber(ConventionError):
    """"#36" with no repo. Issue numbers restart in every repo, so the caller has to look the
    number up (Intake does, across the configured repos)."""

    def __init__(self, number: int, repos: dict[str, str]):
        super().__init__(
            f"Which repo is #{number}? Say e.g. " + " or ".join(f"{p}#{number}" for p in repos) + "."
        )
        self.number = number


def parse_ref(text: str, repos: dict[str, str]) -> IssueRef:
    """`FO-AI/nimbus#42`, `https://github.com/FO-AI/nimbus/issues/42`, `nimbus#42` or `NIM#42`.
    The repo must be one of the configured `repos` (prefix -> owner/repo)."""
    s = (text or "").strip()
    if s.lower().startswith("http"):
        s = re.split(r"[?#]", s, maxsplit=1)[0]  # .../issues/42#issuecomment-1 -> .../issues/42
    known = sorted(repos.values())
    repo: str | None = None
    number: int | None = None
    if m := _ANY_URL_RE.match(s):
        if m.group(3).lower() == "pull":
            raise ConventionError(f"{s} is a pull request, not an issue.")
        repo, number = f"{m.group(1)}/{m.group(2)}", int(m.group(4))
    elif m := _FULL_REF_RE.match(s):
        repo, number = f"{m.group(1)}/{m.group(2)}", int(m.group(3))
    elif m := re.fullmatch(r"#?(\d+)", s):
        raise BareIssueNumber(int(m.group(1)), repos)
    elif m := _SHORT_REF_RE.match(s):
        name, number = m.group(1), int(m.group(2))
        if name.upper() in repos:
            repo = repos[name.upper()]
        else:
            repo = next((r for r in known if r.split("/", 1)[1].lower() == name.lower()), name)
    else:
        raise ConventionError(
            f"Can't read issue {text!r}. Use owner/repo#N, e.g. {known[0] if known else 'FO-AI/nimbus'}#42."
        )
    configured = next((r for r in known if r.lower() == repo.lower()), None)
    if configured is None:
        raise ConventionError(f"{repo} is not a configured repo. Configured: {', '.join(known)}")
    return IssueRef(configured, number)


def encode_key(url: str) -> str:
    return "".join(_KEY_ESCAPES.get(ch, ch) for ch in url)


def decode_key(key: str) -> str:
    return urllib.parse.unquote(key)


def reference_body(ref: IssueRef) -> dict:
    """The `references` fragment for a plannerTaskDetails PATCH."""
    return {
        encode_key(ref.link_url): {
            "@odata.type": REFERENCE_TYPE,
            "alias": str(ref),
            "type": "Other",
        }
    }


def issue_in_url(url: str) -> tuple[str, int] | None:
    """("FO-AI/nimbus", 42) for a GitHub issue URL; None for anything else (SharePoint, PRs)."""
    m = _ISSUE_URL_RE.match(url.strip())
    return (f"{m.group(1)}/{m.group(2)}", int(m.group(3))) if m else None


def link_key(repo: str, number: int, renamed: dict[str, str] | None = None) -> str:
    """Dedup key. Case-insensitive, and a pre-rename repo name maps to its new name."""
    r = repo.lower()
    for old, new in (renamed or {}).items():
        if r == old.lower():
            r = new.lower()
            break
    return f"{r}#{number}"


def issue_line(ref: IssueRef) -> str:
    return f"Issue: {ref}"
