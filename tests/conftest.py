from pathlib import Path

import pytest

from planner_mcp.board import Board
from planner_mcp.config import Config
from planner_mcp.graph import PlannerClient

from .fake_graph import BUCKETS, PLAN, FakePlanner


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    return Config(
        tenant_id="tenant",
        client_id="client",
        plan_id=PLAN,
        buckets=list(BUCKETS),
        prefixes=["NIM", "PIQ", "QUAD", "OPS"],
        tier_priority={"Small": 5, "Medium": 3, "Large": 1},
        token_cache=tmp_path / "cache.json",
        people={"Chris Kim": "u-chris", "Daniel Zhang": "u-daniel"},
        people_lookup=True,
    )


@pytest.fixture
def fake() -> FakePlanner:
    f = FakePlanner()
    f.add_task("[NIM] Foundry provider validated against pinned SDK", "This Week",
               assignee="u-chris", category="category1", priority=3, due="2026-09-26T12:00:00Z",
               checklist=["Pin SDK", "Smoke test"], description="AC: /ask answers via foundry.")
    f.add_task("[PIQ] Sample PD data indexed in the Fabric environment", "Backlog", category="category2")
    f.add_task("[QUAD] Department three onboarded via config only", "Shaping", assignee="u-daniel",
               category="category3")
    f.add_task("[OPS] Old card", "Review", assignee="u-kaiji", category="category4", percent=100)
    return f


@pytest.fixture
def board(cfg: Config, fake: FakePlanner) -> Board:
    client = PlannerClient(PLAN, token=lambda: "TOKEN", transport=fake.transport())
    return Board(cfg, client)
