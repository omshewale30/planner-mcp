"""End-to-end check of the deployable image, run on the laptop before pushing.

Builds the Docker image, runs it the way OpenShift does (random non-root UID, group 0,
writable /data volume, config mounted read-only), and talks to it over HTTP with the
official MCP client, using real Microsoft Graph and GitHub. Only read-only tools are
called, so the board and the repos are not changed.

What it proves, each a line in the report:
  1. `serve` refuses to start without PLANNER_MCP_API_KEY.
  2. A request with no key, or the wrong key, gets 401.
  3. With the key and a non-localhost Host header (as behind the OpenShift route), the
     server lists all 15 tools.
  4. Before a token cache is copied in, a tool call returns the "Not signed in" error.
  5. After the laptop's token cache is copied into /data (the reseed step), with its
     access tokens removed so the refresh token must be used, the same
     running server reads the board: no restart needed.
  6. GitHub intake works with GH_TOKEN and no `gh auth login`. Uses $GH_TOKEN if set (to
     test the token the server will get), else this laptop's `gh auth token`.
  7. The refreshed token cache is written back to /data.

Run:  uv run python e2e/container_check.py
Report: e2e/out/container-check.json (gitignored)
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx
import yaml
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

REPO = Path(__file__).resolve().parent.parent
REPORT = REPO / "e2e" / "out" / "container-check.json"
LAPTOP_CONFIG = Path("~/.config/planner-mcp/config.yaml").expanduser()
IMAGE = "planner-mcp:e2e"
CONTAINER = "planner-mcp-e2e"
OPENSHIFT_LIKE_USER = "1234567:0"  # OpenShift runs images as a random UID in group 0
ROUTE_HOST = "planner-mcp.apps.cloudapps.unc.edu"
API_KEY = "e2e-" + os.urandom(16).hex()
EXPECTED_TOOLS = 15
STARTUP_TIMEOUT_S = 30

checks: list[dict] = []


def record(name: str, passed: bool, detail: str = "") -> None:
    checks.append({"check": name, "passed": passed, "detail": detail})
    print(f"[{'PASS' if passed else 'FAIL'}] {name} {detail}", file=sys.stderr)


def docker(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], capture_output=True, text=True, check=check)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def container_config(workdir: Path) -> Path:
    """The laptop config, with the two paths that differ inside the container."""
    raw = yaml.safe_load(LAPTOP_CONFIG.read_text())
    raw["token_cache"] = "/data/token_cache.json"
    if raw.get("github"):
        raw["github"]["gh_path"] = None
    path = workdir / "config" / "config.yaml"
    path.parent.mkdir()
    path.write_text(yaml.safe_dump(raw))
    return path.parent


def run_container(port: int, config_dir: Path, data_dir: Path, env: dict[str, str]) -> None:
    env_args = [arg for k, v in env.items() for arg in ("-e", f"{k}={v}")]
    docker(
        "run", "-d", "--name", CONTAINER, "--user", OPENSHIFT_LIKE_USER,
        "-p", f"127.0.0.1:{port}:8080",
        "-v", f"{config_dir}:/config:ro", "-v", f"{data_dir}:/data",
        *env_args, IMAGE,
    )


def wait_until_listening(port: int) -> bool:
    deadline = time.monotonic() + STARTUP_TIMEOUT_S
    while time.monotonic() < deadline:
        try:
            httpx.get(f"http://127.0.0.1:{port}/", timeout=1)
            return True
        except httpx.TransportError:
            time.sleep(0.5)
    return False


def check_refuses_without_key(config_dir: Path, data_dir: Path) -> None:
    result = docker(
        "run", "--rm", "--user", OPENSHIFT_LIKE_USER,
        "-v", f"{config_dir}:/config:ro", "-v", f"{data_dir}:/data", IMAGE, check=False,
    )
    passed = result.returncode != 0 and "PLANNER_MCP_API_KEY" in result.stderr
    record("refuses to start without PLANNER_MCP_API_KEY", passed,
           f"exit={result.returncode} stderr={result.stderr.strip()[-200:]!r}")


def check_rejects_bad_keys(url: str) -> None:
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    headers = {"Accept": "application/json, text/event-stream", "Host": ROUTE_HOST}
    no_key = httpx.post(url, json=body, headers=headers)
    record("no key -> 401", no_key.status_code == 401, f"got {no_key.status_code}")
    wrong = httpx.post(url, json=body, headers={**headers, "Authorization": "Bearer nope"})
    record("wrong key -> 401", wrong.status_code == 401, f"got {wrong.status_code}")


async def call_tool(url: str, name: str, arguments: dict | None = None):
    headers = {"Authorization": f"Bearer {API_KEY}", "Host": ROUTE_HOST}
    async with (
        streamablehttp_client(url, headers=headers) as (read, write, _),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        if name == "tools/list":
            return await session.list_tools()
        return await session.call_tool(name, arguments or {})


def tool_text(result) -> str:
    return "".join(getattr(part, "text", "") for part in result.content)


def check_tools_listed(url: str) -> None:
    tools = asyncio.run(call_tool(url, "tools/list")).tools
    record(f"lists {EXPECTED_TOOLS} tools behind a foreign Host header",
           len(tools) == EXPECTED_TOOLS, f"got {len(tools)}")


def check_not_signed_in(url: str) -> None:
    result = asyncio.run(call_tool(url, "board_info"))
    text = tool_text(result)
    record("no token cache -> 'Not signed in' error", "Not signed in" in text, text[:160])


def check_reads_board_after_reseed(url: str, data_dir: Path) -> None:
    """Copies the laptop cache without its access tokens, so the server has to use the
    refresh token: the same path it takes every hour once deployed."""
    laptop_cache = Path(yaml.safe_load(LAPTOP_CONFIG.read_text())["token_cache"]).expanduser()
    cache = json.loads(laptop_cache.read_text())
    cache["AccessToken"] = {}
    seeded = data_dir / "token_cache.json"
    seeded.write_text(json.dumps(cache))
    os.utime(seeded, (0, 0))
    result = asyncio.run(call_tool(url, "board_info"))
    text = tool_text(result)
    passed = not result.isError and '"buckets"' in text
    record("after copying the token cache in, board_info reads the real board", passed,
           text[:160])


def check_github_intake(url: str) -> None:
    result = asyncio.run(call_tool(url, "intake_list"))
    text = tool_text(result)
    passed = not result.isError and '"error"' not in text
    record("intake_list reads GitHub with GH_TOKEN", passed, text[:160])


def check_cache_written_back(data_dir: Path, copied_at: float) -> None:
    cache = data_dir / "token_cache.json"
    has_access_token = bool(json.loads(cache.read_text()).get("AccessToken"))
    passed = cache.stat().st_mtime > copied_at and has_access_token
    record("server wrote the refreshed token cache back to /data", passed,
           f"mtime_after_seed={cache.stat().st_mtime > copied_at} access_token={has_access_token}")


def write_report(image_id: str) -> None:
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps({
        "ran_at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
        "image": IMAGE,
        "image_id": image_id,
        "git_commit": subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True,
                                     text=True, cwd=REPO, check=False).stdout.strip(),
        "passed": all(c["passed"] for c in checks),
        "checks": checks,
    }, indent=2) + "\n")
    print(f"Report: {REPORT}", file=sys.stderr)


def main() -> int:
    build = docker("build", "-t", IMAGE, str(REPO), check=False)
    if build.returncode != 0:
        print(build.stderr[-2000:], file=sys.stderr)
        return 1
    image_id = docker("image", "inspect", "-f", "{{.Id}}", IMAGE).stdout.strip()
    # GH_TOKEN, when set, is the token the server will get: test that one before uploading it.
    gh_token = os.environ.get("GH_TOKEN") or subprocess.run(
        ["gh", "auth", "token"], capture_output=True, text=True, check=True).stdout.strip()

    with tempfile.TemporaryDirectory() as tmp:
        workdir = Path(tmp)
        config_dir = container_config(workdir)
        data_dir = workdir / "data"
        data_dir.mkdir(mode=0o777)
        data_dir.chmod(0o777)  # umask strips group/other bits from mkdir's mode

        check_refuses_without_key(config_dir, data_dir)

        port = free_port()
        url = f"http://127.0.0.1:{port}/mcp"
        docker("rm", "-f", CONTAINER, check=False)
        run_container(port, config_dir, data_dir,
                      {"PLANNER_MCP_API_KEY": API_KEY, "GH_TOKEN": gh_token})
        try:
            if not wait_until_listening(port):
                record("server starts", False, docker("logs", CONTAINER, check=False).stderr)
            else:
                check_rejects_bad_keys(url)
                check_tools_listed(url)
                check_not_signed_in(url)
                copied_at = time.time()
                check_reads_board_after_reseed(url, data_dir)
                check_github_intake(url)
                check_cache_written_back(data_dir, copied_at)
        finally:
            logs = docker("logs", CONTAINER, check=False).stderr
            (REPORT.parent).mkdir(parents=True, exist_ok=True)
            (REPORT.parent / "container.log").write_text(logs.replace(API_KEY, "***"))
            docker("rm", "-f", CONTAINER, check=False)

    write_report(image_id)
    return 0 if all(c["passed"] for c in checks) else 1


if __name__ == "__main__":
    sys.exit(main())
