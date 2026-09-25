"""An in-memory Planner behind httpx.MockTransport. Enough Graph semantics to test on:
etags, 412 on stale etag, /details as a separate resource, appliedCategories, assignments."""

from __future__ import annotations

import json
import re
import uuid

import httpx

PLAN = "PLAN1"
GROUP = "GROUP1"
USERS = {
    "u-chris": {"id": "u-chris", "displayName": "Chris Kim", "mail": "chris@unc.edu"},
    "u-kaiji": {"id": "u-kaiji", "displayName": "Kaiji Fu", "mail": "kaiji@unc.edu"},
    "u-daniel": {"id": "u-daniel", "displayName": "Daniel Zhang", "mail": "daniel@unc.edu"},
}
BUCKETS = ["Backlog", "Shaping", "This Week", "In Progress", "Blocked", "Review"]


class FakePlanner:
    def __init__(self):
        self.etag_n = 0
        self.buckets = {f"b-{i}": {"id": f"b-{i}", "name": n} for i, n in enumerate(BUCKETS)}
        self.categories = {"category1": "NIM", "category2": "PIQ", "category3": "QUAD", "category4": "OPS"}
        self.tasks: dict[str, dict] = {}
        self.details: dict[str, dict] = {}
        self.calls: list[tuple[str, str]] = []
        self.force_412_once = False
        self.allow_user_lookup = True
        self.user_lookups = 0

    # -- helpers
    def _etag(self) -> str:
        self.etag_n += 1
        return f'W/"{self.etag_n}"'

    def bucket_id(self, name: str) -> str:
        return next(b["id"] for b in self.buckets.values() if b["name"] == name)

    def add_task(self, title, bucket="Backlog", assignee=None, category=None, priority=5,
                 percent=0, due=None, checklist=None, description=""):
        tid = f"t-{uuid.uuid4().hex[:6]}"
        t = {
            "id": tid, "planId": PLAN, "bucketId": self.bucket_id(bucket), "title": title,
            "priority": priority, "percentComplete": percent, "dueDateTime": due,
            "assignments": {assignee: {"orderHint": " !"}} if assignee else {},
            "appliedCategories": {category: True} if category else {},
            "createdDateTime": "2026-09-01T00:00:00Z", "hasDescription": bool(description),
            "@odata.etag": self._etag(),
        }
        cl = {str(uuid.uuid4()): {"title": c, "isChecked": False, "orderHint": " !"} for c in (checklist or [])}
        t["checklistItemCount"] = len(cl)
        t["activeChecklistItemCount"] = len(cl)
        self.tasks[tid] = t
        self.details[tid] = {"id": tid, "description": description, "checklist": cl, "@odata.etag": self._etag()}
        return tid

    # -- transport
    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/v1.0")
        self.calls.append((request.method, path))
        assert request.headers.get("Authorization", "").startswith("Bearer "), "no token"
        body = json.loads(request.content) if request.content else {}

        if path == f"/planner/plans/{PLAN}":
            return httpx.Response(200, json={"id": PLAN, "container": {"containerId": GROUP}})
        if path == f"/planner/plans/{PLAN}/details":
            return httpx.Response(200, json={"categoryDescriptions": self.categories})
        if path == f"/planner/plans/{PLAN}/buckets":
            return httpx.Response(200, json={"value": list(self.buckets.values())})
        if path == f"/planner/plans/{PLAN}/tasks":
            return httpx.Response(200, json={"value": list(self.tasks.values())})
        if path == "/me":
            return httpx.Response(200, json={"id": "u-om", "displayName": "Om Shewale", "mail": "om@unc.edu"})
        um = re.fullmatch(r"/users/([^/]+)", path)
        if um:
            self.user_lookups += 1
            if not self.allow_user_lookup:
                return httpx.Response(403, json={"error": {"message": "Insufficient privileges"}})
            u = USERS.get(um.group(1))
            return httpx.Response(200, json=u) if u else httpx.Response(404, json={"error": {"message": "nf"}})

        if path == "/planner/tasks" and request.method == "POST":
            assert body["planId"] == PLAN
            tid = f"t-{uuid.uuid4().hex[:6]}"
            t = {
                "id": tid, "planId": PLAN, "bucketId": body["bucketId"], "title": body["title"],
                "priority": body.get("priority", 5), "percentComplete": 0,
                "dueDateTime": body.get("dueDateTime"), "assignments": body.get("assignments", {}),
                "appliedCategories": body.get("appliedCategories", {}), "hasDescription": False,
                "checklistItemCount": 0, "activeChecklistItemCount": 0,
                "createdDateTime": "2026-09-21T00:00:00Z", "@odata.etag": self._etag(),
            }
            self.tasks[tid] = t
            self.details[tid] = {"id": tid, "description": "", "checklist": {}, "@odata.etag": self._etag()}
            return httpx.Response(201, json=t)

        m = re.fullmatch(r"/planner/tasks/([^/]+)(/details)?", path)
        if not m:
            return httpx.Response(404, json={"error": {"message": f"no route {path}"}})
        tid, is_details = m.group(1), bool(m.group(2))
        store = self.details if is_details else self.tasks
        if tid not in store:
            return httpx.Response(404, json={"error": {"message": "task not found"}})
        obj = store[tid]

        if request.method == "GET":
            return httpx.Response(200, json=obj)
        if request.method == "PATCH":
            if self.force_412_once:
                self.force_412_once = False
                return httpx.Response(412, json={"error": {"message": "forced stale"}})
            if request.headers.get("If-Match") != obj["@odata.etag"]:
                return httpx.Response(412, json={"error": {"message": "The If-Match precondition failed"}})
            if is_details:
                if "description" in body:
                    obj["description"] = body["description"]
                    self.tasks[tid]["hasDescription"] = bool(body["description"])
                if "checklist" in body:
                    for k, v in body["checklist"].items():
                        if v is None:
                            obj["checklist"].pop(k, None)
                        else:
                            obj["checklist"][k] = {kk: vv for kk, vv in v.items() if not kk.startswith("@")}
                    self.tasks[tid]["checklistItemCount"] = len(obj["checklist"])
                    self.tasks[tid]["activeChecklistItemCount"] = sum(
                        1 for c in obj["checklist"].values() if not c.get("isChecked"))
            else:
                if "assignments" in body:
                    for k, v in body["assignments"].items():
                        if v is None:
                            obj["assignments"].pop(k, None)
                        else:
                            obj["assignments"][k] = {"orderHint": v.get("orderHint", " !")}
                    body = {k: v for k, v in body.items() if k != "assignments"}
                obj.update(body)
            obj["@odata.etag"] = self._etag()
            if request.headers.get("Prefer") == "return=representation":
                return httpx.Response(200, json=obj)
            return httpx.Response(204)
        return httpx.Response(405, json={"error": {"message": "method"}})

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)
