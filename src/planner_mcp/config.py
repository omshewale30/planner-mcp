"""Configuration for planner-mcp: one YAML file, validated at startup."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

DEFAULT_PATH = Path("~/.config/planner-mcp/config.yaml")
BASE_SCOPES = ["Tasks.ReadWrite", "User.Read"]
# Optional: lets the server look up assignees it has never seen. Not admin-restricted in
# most tenants, but it is a separate consent, so it is opt-in.
LOOKUP_SCOPE = "User.ReadBasic.All"


class ConfigError(RuntimeError):
    pass


_REPO_RE = re.compile(r"^[\w.-]+/[\w.-]+$")


@dataclass
class GitHubConfig:
    """Bug intake from GitHub Issues, read through Om's `gh` login (no token in this server)."""

    repos: dict[str, str]  # prefix -> "owner/repo"
    gh_path: str | None = None  # None: auto-detect
    intake_types: list[str] = field(default_factory=lambda: ["Bug"])  # org issue types read
    limit_per_repo: int = 50
    timeout_s: float = 20
    renamed: dict[str, str] = field(default_factory=dict)  # old "owner/repo" -> new

    def prefix_for(self, repo: str) -> str | None:
        for prefix, r in self.repos.items():
            if r.lower() == repo.lower():
                return prefix
        return None


@dataclass
class Config:
    tenant_id: str
    client_id: str
    plan_id: str
    buckets: list[str]
    prefixes: list[str]
    tier_priority: dict[str, int]
    token_cache: Path
    people: dict[str, str] = field(default_factory=dict)  # display name -> Entra object id
    people_lookup: bool = False
    auth_flow: str = "interactive"  # or "device_code"
    github: GitHubConfig | None = None  # optional: intake tools are off without it

    @property
    def scopes(self) -> list[str]:
        return BASE_SCOPES + ([LOOKUP_SCOPE] if self.people_lookup else [])

    @property
    def authority(self) -> str:
        return f"https://login.microsoftonline.com/{self.tenant_id}"

    def tier_for_priority(self, priority: int | None) -> str | None:
        """Reverse map: pick the tier whose priority is closest at-or-below the value."""
        if priority is None:
            return None
        for tier, p in sorted(self.tier_priority.items(), key=lambda kv: kv[1]):
            if priority <= p:
                return tier
        return None


def load(path: str | os.PathLike | None = None) -> Config:
    p = Path(path or os.environ.get("PLANNER_MCP_CONFIG") or DEFAULT_PATH).expanduser()
    if not p.exists():
        raise ConfigError(
            f"Config not found at {p}. Copy config.example.yaml there or set PLANNER_MCP_CONFIG."
        )
    raw = yaml.safe_load(p.read_text()) or {}

    missing = [k for k in ("tenant_id", "client_id", "plan_id", "buckets", "prefixes") if not raw.get(k)]
    if missing:
        raise ConfigError(f"Config {p} is missing: {', '.join(missing)}")
    if raw["plan_id"] == "REPLACE_ME":
        raise ConfigError("Config plan_id is still REPLACE_ME.")
    if str(raw.get("auth_flow") or "interactive") not in ("interactive", "device_code"):
        raise ConfigError("auth_flow must be 'interactive' or 'device_code'.")

    prefixes = [str(x).upper() for x in raw["prefixes"]]
    tier_priority = {str(k): int(v) for k, v in (raw.get("tier_priority") or {}).items()} or {
        "Small": 5,
        "Medium": 3,
        "Large": 1,
    }
    return Config(
        tenant_id=str(raw["tenant_id"]),
        client_id=str(raw["client_id"]),
        plan_id=str(raw["plan_id"]),
        buckets=[str(b) for b in raw["buckets"]],
        prefixes=prefixes,
        tier_priority=tier_priority,
        token_cache=Path(raw.get("token_cache") or "~/.config/planner-mcp/token_cache.json").expanduser(),
        people={str(k): str(v) for k, v in (raw.get("people") or {}).items()},
        people_lookup=bool(raw.get("people_lookup", False)),
        auth_flow=str(raw.get("auth_flow") or "interactive"),
        github=_github(raw.get("github"), prefixes),
    )


def _github(raw: dict | None, prefixes: list[str]) -> GitHubConfig | None:
    if not raw:
        return None
    if not isinstance(raw, dict) or not isinstance(raw.get("repos"), dict) or not raw["repos"]:
        raise ConfigError("github.repos must map prefixes to repos, e.g. NIM: FO-AI/nimbus")
    repos = {str(k).upper(): str(v) for k, v in raw["repos"].items()}
    unknown = [k for k in repos if k not in prefixes]
    if unknown:
        raise ConfigError(f"github.repos has prefixes not in `prefixes`: {unknown}")
    renamed = {str(k): str(v) for k, v in (raw.get("renamed") or {}).items()}
    bad = [r for r in [*repos.values(), *renamed, *renamed.values()] if not _REPO_RE.match(r)]
    if bad:
        raise ConfigError(f"github repos must look like owner/name: {bad}")
    if "intake_labels" in raw:
        raise ConfigError(
            "github.intake_labels was replaced by intake_types: bugs are found by their org "
            "issue type now. Use `intake_types: [Bug]`."
        )
    types = ["Bug"] if raw.get("intake_types") is None else [str(x) for x in raw["intake_types"]]
    if not types:
        raise ConfigError("github.intake_types needs at least one issue type (e.g. [Bug]).")
    limit = int(raw.get("limit_per_repo") or 50)
    if not 1 <= limit <= 200:
        raise ConfigError("github.limit_per_repo must be between 1 and 200.")
    timeout = float(raw.get("timeout_s") or 20)
    if timeout <= 0:
        raise ConfigError("github.timeout_s must be positive.")
    return GitHubConfig(
        repos=repos,
        gh_path=str(raw["gh_path"]) if raw.get("gh_path") else None,
        intake_types=types,
        limit_per_repo=limit,
        timeout_s=timeout,
        renamed=renamed,
    )

