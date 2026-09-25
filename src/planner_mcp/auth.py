"""Delegated auth as Om via MSAL, with an on-disk token cache.

Default flow is interactive browser sign-in (authorization code + PKCE on a localhost
redirect). Device-code flow is available as a fallback (`auth_flow: device_code`) but
UNC's Conditional Access blocks it, which surfaces as "You don't have access to this...
authentication flow that is restricted by your admin".

Sign-in only ever happens from `planner-mcp login`. In server mode a missing or dead token
is an error that says so, rather than a browser window popping out of a background process.

Everything user-facing goes to stderr: stdout is the MCP transport.
"""

from __future__ import annotations

import atexit
import logging
import os
import sys
from pathlib import Path

import msal

from .config import Config

log = logging.getLogger(__name__)


class AuthError(RuntimeError):
    pass


class TokenProvider:
    def __init__(self, cfg: Config, allow_interactive: bool = False):
        self._cfg = cfg
        self._allow_interactive = allow_interactive
        self._cache = msal.SerializableTokenCache()
        self._cache_path: Path = cfg.token_cache
        if self._cache_path.exists():
            self._cache.deserialize(self._cache_path.read_text())
        atexit.register(self._persist)
        self._app = msal.PublicClientApplication(
            cfg.client_id, authority=cfg.authority, token_cache=self._cache
        )

    def _persist(self) -> None:
        if not self._cache.has_state_changed:
            return
        self._cache_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._cache_path.with_suffix(".tmp")
        tmp.write_text(self._cache.serialize())
        os.chmod(tmp, 0o600)
        tmp.replace(self._cache_path)

    def token(self) -> str:
        accounts = self._app.get_accounts()
        if accounts:
            result = self._app.acquire_token_silent(self._cfg.scopes, account=accounts[0])
            if result and "access_token" in result:
                self._persist()
                return result["access_token"]

        if not self._allow_interactive:
            raise AuthError(
                "Not signed in (or the cached token can no longer be refreshed). "
                "Run `planner-mcp login` in a terminal, then retry."
            )

        if self._cfg.auth_flow == "device_code":
            result = self._device_code()
        else:
            result = self._interactive()

        if "access_token" not in result:
            raise AuthError(
                f"Sign-in failed: {result.get('error')}: {result.get('error_description', result)}"
            )
        self._persist()
        return result["access_token"]

    def _interactive(self) -> dict:
        print(
            "[planner-mcp] Opening your browser to sign in. The app registration needs the "
            "redirect URI http://localhost under 'Mobile and desktop applications'.",
            file=sys.stderr,
            flush=True,
        )
        return self._app.acquire_token_interactive(
            scopes=self._cfg.scopes,
            prompt="select_account",
            timeout=300,
            success_template=(
                "<html><body style='font-family:sans-serif'><h3>planner-mcp: signed in.</h3>"
                "<p>You can close this tab and return to the terminal.</p></body></html>"
            ),
        )

    def _device_code(self) -> dict:
        flow = self._app.initiate_device_flow(scopes=self._cfg.scopes)
        if "user_code" not in flow:
            raise AuthError(f"Device flow failed: {flow.get('error_description', flow)}")
        print(f"\n[planner-mcp] {flow['message']}\n", file=sys.stderr, flush=True)
        return self._app.acquire_token_by_device_flow(flow)

    def signed_in_as(self) -> str | None:
        accounts = self._app.get_accounts()
        return accounts[0].get("username") if accounts else None
