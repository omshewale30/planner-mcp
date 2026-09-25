"""Board Conventions v1.1 enforced server-side, so a bad card cannot be written."""

from __future__ import annotations

import re

MAX_CHECKLIST = 20
_PREFIX_RE = re.compile(r"^\[([A-Z]+)\]\s+\S")


class ConventionError(ValueError):
    pass


def check_title(title: str, prefixes: list[str]) -> str:
    title = title.strip()
    m = _PREFIX_RE.match(title)
    if not m:
        raise ConventionError(
            f"Title must start with a prefix like {' '.join(f'[{p}]' for p in prefixes)} "
            f"followed by an outcome: {title!r}"
        )
    if m.group(1) not in prefixes:
        raise ConventionError(f"Unknown prefix [{m.group(1)}]. Allowed: {', '.join(prefixes)}")
    return m.group(1)


def check_checklist(items: list[str]) -> list[str]:
    cleaned = [i.strip() for i in items if i and i.strip()]
    if len(cleaned) > MAX_CHECKLIST:
        raise ConventionError(f"Checklist has {len(cleaned)} items; the maximum is {MAX_CHECKLIST}.")
    return cleaned


def check_single_assignee(assignees: list[str] | None) -> str | None:
    if not assignees:
        return None
    if len(assignees) > 1:
        raise ConventionError("One assignee per card. Two owners means no owner.")
    return assignees[0]


def check_bucket(name: str, buckets: list[str]) -> str:
    for b in buckets:
        if b.lower() == name.strip().lower():
            return b
    raise ConventionError(f"Unknown bucket {name!r}. Buckets: {', '.join(buckets)}")


def check_tier(tier: str | None, tier_priority: dict[str, int]) -> str:
    if tier is None:
        return "Small"
    for t in tier_priority:
        if t.lower() == tier.strip().lower():
            return t
    raise ConventionError(f"Unknown tier {tier!r}. Tiers: {', '.join(tier_priority)}")
