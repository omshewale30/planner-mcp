"""The board as Claude sees it: cards, buckets, people. Conventions enforced on every write."""

from __future__ import annotations

import datetime as dt
import logging
import uuid
from dataclasses import dataclass, field

from . import conventions as conv
from .config import Config
from .graph import GraphError, PlannerClient

log = logging.getLogger(__name__)

ASSIGNMENT_TYPE = "#microsoft.graph.plannerAssignment"
CHECKLIST_TYPE = "#microsoft.graph.plannerChecklistItem"


@dataclass
class BoardMeta:
    bucket_by_name: dict[str, str] = field(default_factory=dict)  # name -> id
    bucket_by_id: dict[str, str] = field(default_factory=dict)  # id -> name
    category_by_prefix: dict[str, str] = field(default_factory=dict)  # "NIM" -> "category3"
    prefix_by_category: dict[str, str] = field(default_factory=dict)  # "category3" -> "NIM"
    user_by_id: dict[str, dict] = field(default_factory=dict)  # id -> {displayName, mail}
    unknown_user_ids: set[str] = field(default_factory=set)  # seen on cards, not resolvable


class Board:
    def __init__(self, cfg: Config, client: PlannerClient):
        self.cfg = cfg
        self.client = client
        self._meta: BoardMeta | None = None

    # ---- metadata (buckets, labels, people) --------------------------------

    def meta(self, refresh: bool = False) -> BoardMeta:
        if self._meta and not refresh:
            return self._meta
        m = BoardMeta()
        # Bucket names match case-insensitively; the config spelling is what Claude sees.
        planner = {b["name"].strip().lower(): b for b in self.client.buckets()}
        missing = [b for b in self.cfg.buckets if b.strip().lower() not in planner]
        if missing:
            raise RuntimeError(
                f"Plan is missing buckets named in config: {missing}. "
                f"Planner has: {sorted(b['name'] for b in planner.values())}"
            )
        for name in self.cfg.buckets:
            b = planner[name.strip().lower()]
            m.bucket_by_name[name] = b["id"]
            m.bucket_by_id[b["id"]] = name
        for key, b in planner.items():  # buckets Planner has that config does not name
            if b["id"] not in m.bucket_by_id:
                m.bucket_by_name[b["name"]] = b["id"]
                m.bucket_by_id[b["id"]] = b["name"]

        cats = (self.client.plan_details().get("categoryDescriptions") or {})
        for key, label in cats.items():
            if not label:
                continue
            up = str(label).strip().upper()
            if up in self.cfg.prefixes:
                m.category_by_prefix[up] = key
                m.prefix_by_category[key] = up
        unlabelled = [p for p in self.cfg.prefixes if p not in m.category_by_prefix]
        if unlabelled:
            log.warning("No Planner label matches prefix(es) %s; cards will carry no label.", unlabelled)

        # People: the config map is authoritative (no extra Graph permission needed).
        for name, uid in self.cfg.people.items():
            m.user_by_id[uid] = {"displayName": name, "mail": None}
        try:
            me = self.client.me()
            m.user_by_id.setdefault(me["id"], {"displayName": me.get("displayName"), "mail": me.get("mail")})
            m.user_by_id[me["id"]]["is_me"] = True
        except GraphError as e:  # /me needs only User.Read; failing here means auth is off
            log.warning("Could not read /me (%s)", e)
        self._meta = m
        return m

    def resolve_user(self, name_or_id: str, _retry: bool = True) -> str:
        m = self.meta()
        if name_or_id in m.user_by_id:
            return name_or_id
        q = name_or_id.strip().lower()
        if q == "me":
            mine = [uid for uid, u in m.user_by_id.items() if u.get("is_me")]
            if mine:
                return mine[0]
        hits = [
            uid
            for uid, u in m.user_by_id.items()
            if q in (u.get("displayName") or "").lower() or q == (u.get("mail") or "").lower()
        ]
        if len(hits) == 1:
            return hits[0]
        if not hits and _retry and self.cfg.people_lookup:
            # Someone already assigned on the board is resolvable; warm the cache once.
            self.cards(include_completed=True)
            return self.resolve_user(name_or_id, _retry=False)
        if not hits:
            raise conv.ConventionError(
            f"No team member matches {name_or_id!r}. Known: "
            f"{sorted(u['displayName'] for u in m.user_by_id.values() if u.get('displayName'))}. "
            "Add people to config `people` (display name -> Entra object id)."
        )
        names = [m.user_by_id[h]["displayName"] for h in hits]
        raise conv.ConventionError(f"{name_or_id!r} is ambiguous: {names}")

    # ---- reading -----------------------------------------------------------

    def _card(self, t: dict) -> dict:
        m = self.meta()
        assignees = list((t.get("assignments") or {}).keys())
        labels = [m.prefix_by_category.get(k, k) for k, v in (t.get("appliedCategories") or {}).items() if v]
        due = t.get("dueDateTime")
        return {
            "id": t["id"],
            "title": t.get("title"),
            "bucket": m.bucket_by_id.get(t.get("bucketId"), t.get("bucketId")),
            "labels": labels,
            "tier": self.cfg.tier_for_priority(t.get("priority")),
            "priority": t.get("priority"),
            "assignee": self._display(assignees[0]) if assignees else None,
            "assignee_id": assignees[0] if assignees else None,
            "extra_assignees": [self._display(a) for a in assignees[1:]],
            "due": due[:10] if due else None,
            "percent_complete": t.get("percentComplete", 0),
            "completed": t.get("percentComplete", 0) == 100,
            "checklist_total": t.get("checklistItemCount", 0),
            "checklist_open": t.get("activeChecklistItemCount", 0),
            "has_description": bool(t.get("hasDescription")),
            "created": (t.get("createdDateTime") or "")[:10] or None,
            "completed_on": (t.get("completedDateTime") or "")[:10] or None,
            "etag": t.get("@odata.etag"),
        }

    def _display(self, user_id: str) -> str:
        m = self.meta()
        u = m.user_by_id.get(user_id)
        if u is None and self.cfg.people_lookup and user_id not in m.unknown_user_ids:
            try:
                g = self.client.user(user_id)
                u = {"displayName": g.get("displayName"), "mail": g.get("mail") or g.get("userPrincipalName")}
                m.user_by_id[user_id] = u
            except GraphError as e:
                log.info("Cannot resolve user %s (%s); add them to config `people`.", user_id, e)
                m.unknown_user_ids.add(user_id)
        elif u is None:
            m.unknown_user_ids.add(user_id)
        return (u or {}).get("displayName") or user_id

    def cards(self, include_completed: bool = False) -> list[dict]:
        out = [self._card(t) for t in self.client.tasks()]
        if not include_completed:
            out = [c for c in out if not c["completed"]]
        return out

    def snapshot(self, include_completed: bool = False) -> dict:
        m = self.meta()
        by_bucket: dict[str, list[dict]] = {b: [] for b in self.cfg.buckets}
        for c in self.cards(include_completed):
            by_bucket.setdefault(c["bucket"], []).append(c)
        for lst in by_bucket.values():
            lst.sort(key=lambda c: (c["due"] or "9999", c["title"] or ""))
        load: dict[str, dict[str, int]] = {}
        for c in self.cards(False):
            who = c["assignee"] or "(unassigned)"
            load.setdefault(who, {}).setdefault(c["bucket"], 0)
            load[who][c["bucket"]] += 1
        return {
            "plan_id": self.cfg.plan_id,
            "as_of": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
            "buckets": by_bucket,
            "load_by_person": load,
            "people": sorted(u["displayName"] for u in m.user_by_id.values() if u.get("displayName")),
        }

    def card(self, task_id: str) -> dict:
        c = self._card(self.client.task(task_id))
        d = self.client.task_details(task_id)
        items = sorted(
            (d.get("checklist") or {}).items(), key=lambda kv: kv[1].get("orderHint", "")
        )
        c["description"] = d.get("description") or ""
        c["checklist"] = [
            {"id": k, "title": v.get("title"), "checked": bool(v.get("isChecked"))} for k, v in items
        ]
        c["details_etag"] = d.get("@odata.etag")
        return c

    # ---- writing -----------------------------------------------------------

    def create(
        self,
        title: str,
        bucket: str = "Backlog",
        tier: str | None = None,
        assignee: str | None = None,
        due: str | None = None,
        description: str | None = None,
        checklist: list[str] | None = None,
    ) -> dict:
        m = self.meta()
        prefix = conv.check_title(title, self.cfg.prefixes)
        bucket_name = conv.check_bucket(bucket, self.cfg.buckets)
        tier_name = conv.check_tier(tier, self.cfg.tier_priority)
        items = conv.check_checklist(checklist or [])

        body: dict = {
            "bucketId": m.bucket_by_name[bucket_name],
            "title": title.strip(),
            "priority": self.cfg.tier_priority[tier_name],
        }
        cat = m.category_by_prefix.get(prefix)
        if cat:
            body["appliedCategories"] = {cat: True}
        if assignee:
            body["assignments"] = {
                self.resolve_user(assignee): {"@odata.type": ASSIGNMENT_TYPE, "orderHint": " !"}
            }
        if due:
            body["dueDateTime"] = _to_datetime(due)

        task = self.client.create_task(body)
        if description or items:
            details_body: dict = {}
            if description:
                details_body["description"] = description
            if items:
                details_body["checklist"] = _checklist_body(items)
            self.client.patch_details(task["id"], details_body)
        return self.card(task["id"])

    def move(self, task_id: str, bucket: str) -> dict:
        m = self.meta()
        bucket_name = conv.check_bucket(bucket, self.cfg.buckets)
        self.client.patch_task(task_id, {"bucketId": m.bucket_by_name[bucket_name]})
        return self._card(self.client.task(task_id))

    def update(
        self,
        task_id: str,
        assignee: str | None = None,
        clear_assignee: bool = False,
        due: str | None = None,
        clear_due: bool = False,
        tier: str | None = None,
        percent_complete: int | None = None,
        title: str | None = None,
    ) -> dict:
        body: dict = {}
        current = self.client.task(task_id)
        if title is not None:
            conv.check_title(title, self.cfg.prefixes)
            body["title"] = title.strip()
        if assignee or clear_assignee:
            assignments = {uid: None for uid in (current.get("assignments") or {})}
            if assignee:
                assignments[self.resolve_user(assignee)] = {
                    "@odata.type": ASSIGNMENT_TYPE,
                    "orderHint": " !",
                }
            body["assignments"] = assignments
        if due:
            body["dueDateTime"] = _to_datetime(due)
        elif clear_due:
            body["dueDateTime"] = None
        if tier is not None:
            body["priority"] = self.cfg.tier_priority[conv.check_tier(tier, self.cfg.tier_priority)]
        if percent_complete is not None:
            if percent_complete not in (0, 50, 100):
                raise conv.ConventionError("percent_complete must be 0, 50 or 100.")
            body["percentComplete"] = percent_complete
        if not body:
            raise conv.ConventionError("Nothing to update.")
        self.client.patch_task(task_id, body, etag=current.get("@odata.etag"))
        return self._card(self.client.task(task_id))

    def set_checklist(self, task_id: str, items: list[str], replace: bool = True) -> dict:
        items = conv.check_checklist(items)
        d = self.client.task_details(task_id)
        existing = d.get("checklist") or {}
        body: dict = {}
        if replace:
            body = {k: None for k in existing}
            if len(items) > conv.MAX_CHECKLIST:
                raise conv.ConventionError(f"Checklist maximum is {conv.MAX_CHECKLIST}.")
        elif len(existing) + len(items) > conv.MAX_CHECKLIST:
            raise conv.ConventionError(
                f"Adding {len(items)} to {len(existing)} exceeds {conv.MAX_CHECKLIST}."
            )
        body.update(_checklist_body(items))
        self.client.patch_details(task_id, {"checklist": body}, etag=d.get("@odata.etag"))
        return self.card(task_id)

    def add_note(self, task_id: str, text: str) -> dict:
        d = self.client.task_details(task_id)
        stamp = dt.datetime.now(dt.UTC).date().isoformat()
        existing = (d.get("description") or "").rstrip()
        new = f"{existing}\n\n[{stamp}] {text.strip()}" if existing else f"[{stamp}] {text.strip()}"
        self.client.patch_details(task_id, {"description": new}, etag=d.get("@odata.etag"))
        return self.card(task_id)

    def set_description(self, task_id: str, text: str) -> dict:
        d = self.client.task_details(task_id)
        self.client.patch_details(task_id, {"description": text}, etag=d.get("@odata.etag"))
        return self.card(task_id)


def _checklist_body(items: list[str]) -> dict:
    # " !" is Planner's "append" order hint. Sending several in one PATCH keeps
    # request order in practice; if Planner ever reorders, number the titles.
    return {
        str(uuid.uuid4()): {
            "@odata.type": CHECKLIST_TYPE,
            "title": title,
            "isChecked": False,
            "orderHint": " !",
        }
        for title in items
    }


def _to_datetime(day: str) -> str:
    """Accept YYYY-MM-DD (or full ISO) and return the ISO instant Planner expects."""
    day = day.strip()
    if len(day) == 10:
        return f"{day}T12:00:00Z"
    return day
