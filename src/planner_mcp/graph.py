"""Thin Microsoft Graph client for Planner. Handles etags, paging and 412 retries.

Graph facts that matter here:
- Every PATCH/DELETE on a plannerTask or plannerTaskDetails needs `If-Match: <@odata.etag>`.
- The task and its /details are separate resources with separate etags.
- `Prefer: return=representation` makes PATCH return the updated object (else 204).
- Labels are `appliedCategories: {category1: true}`; names live on the plan's details.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

import httpx

GRAPH = "https://graph.microsoft.com/v1.0"
log = logging.getLogger(__name__)


class GraphError(RuntimeError):
    def __init__(self, status: int, message: str, body: Any = None):
        super().__init__(f"Graph {status}: {message}")
        self.status = status
        self.body = body


class PlannerClient:
    def __init__(
        self,
        plan_id: str,
        token: Callable[[], str],
        transport: httpx.BaseTransport | None = None,
    ):
        self.plan_id = plan_id
        self._token = token
        self._http = httpx.Client(base_url=GRAPH, timeout=30, transport=transport)

    # ---- low level -------------------------------------------------------

    def _request(
        self,
        method: str,
        path: str,
        json: dict | None = None,
        etag: str | None = None,
        prefer_repr: bool = False,
        params: dict | None = None,
    ) -> Any:
        headers = {"Authorization": f"Bearer {self._token()}"}
        if etag:
            headers["If-Match"] = etag
        if prefer_repr:
            headers["Prefer"] = "return=representation"
        r = self._http.request(method, path, json=json, headers=headers, params=params)
        if r.status_code >= 400:
            try:
                msg = r.json().get("error", {}).get("message", r.text)
            except ValueError:
                msg = r.text
            raise GraphError(r.status_code, msg, r.text)
        if r.status_code == 204 or not r.content:
            return None
        return r.json()

    def _get_all(self, path: str, params: dict | None = None) -> list[dict]:
        out: list[dict] = []
        data = self._request("GET", path, params=params)
        while True:
            out.extend(data.get("value", []))
            nxt = data.get("@odata.nextLink")
            if not nxt:
                return out
            data = self._request("GET", nxt)

    def _patch_with_retry(self, path: str, body: dict, etag: str | None) -> dict:
        """PATCH with the etag we have; on 412 refetch the resource and try once more."""
        if etag is None:
            etag = self._request("GET", path)["@odata.etag"]
        try:
            return self._request("PATCH", path, json=body, etag=etag, prefer_repr=True)
        except GraphError as e:
            if e.status != 412:
                raise
            log.info("412 on %s, refetching etag and retrying once", path)
            fresh = self._request("GET", path)["@odata.etag"]
            return self._request("PATCH", path, json=body, etag=fresh, prefer_repr=True)

    # ---- plan ------------------------------------------------------------

    def plan(self) -> dict:
        return self._request("GET", f"/planner/plans/{self.plan_id}")

    def plan_details(self) -> dict:
        return self._request("GET", f"/planner/plans/{self.plan_id}/details")

    def buckets(self) -> list[dict]:
        return self._get_all(f"/planner/plans/{self.plan_id}/buckets")

    def tasks(self) -> list[dict]:
        return self._get_all(f"/planner/plans/{self.plan_id}/tasks")

    # ---- tasks -----------------------------------------------------------

    def task(self, task_id: str) -> dict:
        return self._request("GET", f"/planner/tasks/{task_id}")

    def task_details(self, task_id: str) -> dict:
        return self._request("GET", f"/planner/tasks/{task_id}/details")

    def create_task(self, body: dict) -> dict:
        body = {"planId": self.plan_id, **body}
        return self._request("POST", "/planner/tasks", json=body)

    def patch_task(self, task_id: str, body: dict, etag: str | None = None) -> dict:
        return self._patch_with_retry(f"/planner/tasks/{task_id}", body, etag)

    def patch_details(self, task_id: str, body: dict, etag: str | None = None) -> dict:
        return self._patch_with_retry(f"/planner/tasks/{task_id}/details", body, etag)

    # ---- people ----------------------------------------------------------

    def user(self, user_id: str) -> dict:
        """Needs User.ReadBasic.All; without it Graph answers 403 and the caller falls back."""
        return self._request(
            "GET", f"/users/{user_id}", params={"$select": "id,displayName,mail,userPrincipalName"}
        )

    def me(self) -> dict:
        return self._request("GET", "/me", params={"$select": "id,displayName,mail"})
