import datetime as dt
from pathlib import Path

import pytest

from planner_mcp.board import Board
from planner_mcp.config import Config, GitHubConfig
from planner_mcp.github import GitHubClient
from planner_mcp.graph import PlannerClient
from planner_mcp.intake import Intake

from .fake_gh import FakeGh
from .fake_graph import BUCKETS, PLAN, FakePlanner

NOW = dt.datetime(2026, 9, 25, 12, 0, tzinfo=dt.UTC)
BUG_FORM = """### What happened

POST /ask/ with a trailing slash returns 500.

### Expected

Same answer as /ask.

### Steps to reproduce

1. curl -X POST https://nimbus-dev/ask/
2. See 500

### Environment

dev

### Link or commit

_No response_

### Severity

{sev}
"""


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
        github=GitHubConfig(
            repos={"NIM": "FO-AI/nimbus", "PIQ": "FO-AI/position-iq", "QUAD": "FO-AI/benny"}
        ),
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


@pytest.fixture
def gh_fake() -> FakeGh:
    g = FakeGh()
    g.add_issue("FO-AI/nimbus", 42, "500 on /ask with trailing slash",
                body=BUG_FORM.format(sev="S2 - wrong behavior, workaround exists"),
                created="2026-09-20T12:00:00Z",
                comments=[("chriskim", "Also happens on /search/.")])
    g.add_issue("FO-AI/nimbus", 43, "Typo on the sign-in page",
                body=BUG_FORM.format(sev="S3 - cosmetic or minor"), created="2026-09-10T12:00:00Z")
    g.add_issue("FO-AI/nimbus", 44, "Dark mode", labels=("bug",), type="Feature")  # mislabelled
    g.add_issue("FO-AI/nimbus", 40, "Old crash", state="CLOSED")
    g.add_issue("FO-AI/position-iq", 19, "PD data missing in Fabric env",
                body=BUG_FORM.format(sev="S1 - blocks users or a pilot"),
                created="2026-09-22T12:00:00Z", author="eshaam",
                fields={"Product": "PIQ", "Area": "API"})
    g.add_pr("FO-AI/position-iq", 33, "Fix the 403 on the first Export click")
    g.add_issue("FO-AI/benny", 1, "Understand the Benny copilot env", labels=("documentation",),
                type="Task")
    g.add_issue("FO-AI/benny", 5, "Copilot answers in the wrong language", labels=(),
                body="It answers in Spanish sometimes.", created="2026-09-01T12:00:00Z")  # no label
    return g


@pytest.fixture
def intake(cfg: Config, board: Board, gh_fake: FakeGh) -> Intake:
    return Intake(cfg, board, GitHubClient(cfg.github, runner=gh_fake), clock=lambda: NOW)
