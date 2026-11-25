"""`planner-mcp serve`: the same MCP tools over Streamable HTTP, for hosting the server.

Any MCP client that can send a header (Codex, Claude Code, Claude desktop through
mcp-remote) connects with `Authorization: Bearer <PLANNER_MCP_API_KEY>`. Without the key
anyone who finds the URL could edit the board and close issues as Om, so the server
refuses to start without one.

Stateless JSON responses, because the hosting platform idles and restarts the pod and
its router cuts long-lived streams: no session has to survive between requests.
"""

from __future__ import annotations

import hmac
import os

import uvicorn
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.types import ASGIApp, Receive, Scope, Send

API_KEY_ENV = "PLANNER_MCP_API_KEY"
PORT_ENV = "PORT"
DEFAULT_PORT = 8080
LISTEN_ALL_INTERFACES = "0.0.0.0"  # inside a container; the platform's router is the edge


class MissingApiKey(RuntimeError):
    pass


class RequireBearer:
    """Rejects HTTP requests whose Authorization header is not `Bearer <api_key>`."""

    def __init__(self, app: ASGIApp, api_key: str):
        self._app = app
        self._expected = f"Bearer {api_key}".encode()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and not self._authorized(scope):
            await send({"type": "http.response.start", "status": 401,
                        "headers": [(b"content-type", b"text/plain")]})
            await send({"type": "http.response.body", "body": b"Missing or wrong API key.\n"})
            return
        await self._app(scope, receive, send)

    def _authorized(self, scope: Scope) -> bool:
        given = dict(scope["headers"]).get(b"authorization", b"")
        return hmac.compare_digest(given, self._expected)


def serve(mcp: FastMCP) -> None:
    api_key = os.environ.get(API_KEY_ENV, "").strip()
    if not api_key:
        raise MissingApiKey(
            f"{API_KEY_ENV} is not set. `serve` exposes the board on the network and "
            "will not start without a key for callers to present."
        )
    mcp.settings.stateless_http = True
    mcp.settings.json_response = True
    # FastMCP turns on localhost-only Host checking by default. The route's hostname is
    # whatever the platform assigns; the API key is the access control here.
    mcp.settings.transport_security = TransportSecuritySettings(
        enable_dns_rebinding_protection=False
    )
    app = RequireBearer(mcp.streamable_http_app(), api_key)
    port = int(os.environ.get(PORT_ENV) or DEFAULT_PORT)
    uvicorn.run(app, host=LISTEN_ALL_INTERFACES, port=port, log_level="info")
