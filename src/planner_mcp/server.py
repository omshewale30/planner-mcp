"""planner-mcp: the FO-AI Planner board as MCP tools, over stdio.

Run:  planner-mcp            (after `uv sync`)  or  python -m planner_mcp
Config: ~/.config/planner-mcp/config.yaml  (see config.example.yaml)
"""

from __future__ import annotations

import logging
import sys
from typing import Any

from mcp.server.fastmcp import FastMCP

from . import config as cfgmod
from .auth import AuthError, TokenProvider
from .board import Board
from .conventions import ConventionError
from .graph import GraphError, PlannerClient

logging.basicConfig(
    level=logging.INFO,
    stream=sys.stderr,  # stdout is the MCP transport; never print there
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("planner_mcp")

INSTRUCTIONS = """\
FO-AI intern board (Microsoft Planner). Six buckets: Backlog, Shaping, This Week,
In Progress, Blocked, Review. No Done bucket: completed cards have percent_complete 100.
Titles carry a prefix [NIM] [PIQ] [QUAD] [OPS]. Tier (Small/Medium/Large) is stored as
Planner priority. One assignee per card. Checklist max 20 items; steps are never cards.
Call board_snapshot first; it returns every open card by bucket plus load per person.
"""

mcp = FastMCP("planner-board", instructions=INSTRUCTIONS)
_board: Board | None = None


def board() -> Board:
    global _board
    if _board is None:
        cfg = cfgmod.load()
        tokens = TokenProvider(cfg)
        _board = Board(cfg, PlannerClient(cfg.plan_id, tokens.token))
        log.info("planner-mcp ready; plan %s", cfg.plan_id)
    return _board


def _run(thunk) -> Any:
    """Run a zero-arg callable; convention/Graph/config failures come back as data."""
    try:
        return thunk()
    except ConventionError as e:
        return {"error": "convention", "message": str(e)}
    except GraphError as e:
        return {"error": "graph", "status": e.status, "message": str(e)}
    except cfgmod.ConfigError as e:
        return {"error": "config", "message": str(e)}
    except AuthError as e:
        return {"error": "auth", "message": str(e)}


# ---- read ------------------------------------------------------------------


@mcp.tool()
def board_snapshot(include_completed: bool = False) -> dict:
    """Every card on the board grouped by bucket, plus open-card load per person.
    Start here for Monday planning, Friday close, or any "what is in Blocked" question."""
    return _run(lambda: board().snapshot(include_completed))


@mcp.tool()
def list_tasks(
    bucket: str | None = None,
    assignee: str | None = None,
    label: str | None = None,
    include_completed: bool = False,
) -> list[dict]:
    """Cards filtered by bucket name, assignee display name (substring), and/or label prefix
    such as NIM, PIQ, QUAD, OPS."""

    def go():
        out = board().cards(include_completed)
        if bucket:
            out = [c for c in out if (c["bucket"] or "").lower() == bucket.lower()]
        if assignee:
            out = [c for c in out if assignee.lower() in (c["assignee"] or "").lower()]
        if label:
            out = [c for c in out if label.strip("[]").upper() in c["labels"]]
        return out

    return _run(go)


@mcp.tool()
def get_task(task_id: str) -> dict:
    """One card in full: fields, description (notes) and checklist items."""
    return _run(lambda: board().card(task_id))


@mcp.tool()
def board_info() -> dict:
    """Buckets, label map, known people, and any assignee ids on the board that could not be
    resolved to a name (add those to config `people`). Refreshes metadata."""

    def go():
        b = board()
        m = b.meta(refresh=True)
        return {
            "plan_id": b.cfg.plan_id,
            "buckets": b.cfg.buckets,
            "labels": m.prefix_by_category,
            "tiers": b.cfg.tier_priority,
            "people": sorted(u["displayName"] for u in m.user_by_id.values() if u.get("displayName")),
            "unresolved_assignee_ids": sorted(m.unknown_user_ids),
            "people_lookup": b.cfg.people_lookup,
        }

    return _run(go)


# ---- write -----------------------------------------------------------------


@mcp.tool()
def create_task(
    title: str,
    bucket: str = "Backlog",
    tier: str = "Small",
    assignee: str | None = None,
    due: str | None = None,
    description: str | None = None,
    checklist: list[str] | None = None,
) -> dict:
    """Create a card. Title must start with [NIM] [PIQ] [QUAD] or [OPS] and name an outcome.
    Label is set from the prefix. Tier maps to Planner priority. due is YYYY-MM-DD.
    Checklist items are steps (max 20). Returns the created card."""
    return _run(lambda: board().create(title, bucket, tier, assignee, due, description, checklist))


@mcp.tool()
def move_task(task_id: str, bucket: str) -> dict:
    """Move a card to another bucket (Backlog, Shaping, This Week, In Progress, Blocked, Review)."""
    return _run(lambda: board().move(task_id, bucket))


@mcp.tool()
def update_task(
    task_id: str,
    assignee: str | None = None,
    clear_assignee: bool = False,
    due: str | None = None,
    clear_due: bool = False,
    tier: str | None = None,
    percent_complete: int | None = None,
    title: str | None = None,
) -> dict:
    """Assign (replaces any existing assignee), set or clear the due date, change tier,
    retitle, or tick the card with percent_complete=100."""
    return _run(
        lambda: board().update(
            task_id,
            assignee=assignee,
            clear_assignee=clear_assignee,
            due=due,
            clear_due=clear_due,
            tier=tier,
            percent_complete=percent_complete,
            title=title,
        )
    )


@mcp.tool()
def set_checklist(task_id: str, items: list[str], replace: bool = True) -> dict:
    """Write the card's checklist. replace=True swaps the whole list; False appends. Max 20."""
    return _run(lambda: board().set_checklist(task_id, items, replace))


@mcp.tool()
def add_note(task_id: str, text: str) -> dict:
    """Append a dated line to the card's notes, e.g. a carry-back reason or a decision
    pasted from Teams."""
    return _run(lambda: board().add_note(task_id, text))


@mcp.tool()
def set_description(task_id: str, text: str) -> dict:
    """Replace the card's notes wholesale (acceptance criteria, problem statement, sketch)."""
    return _run(lambda: board().set_description(task_id, text))


def login() -> int:
    """`planner-mcp login`: sign in (device code), verify the plan, print a summary. Exits.
    This is the only mode meant for a terminal; plain `planner-mcp` speaks MCP on stdio and
    is started by Claude desktop, not by hand."""
    cfg = cfgmod.load()
    tokens = TokenProvider(cfg, allow_interactive=True)
    tokens.token()
    b = Board(cfg, PlannerClient(cfg.plan_id, tokens.token))
    m = b.meta()
    cards = b.cards(include_completed=False)
    err = sys.stderr
    print(f"Signed in as: {tokens.signed_in_as()}", file=err)
    print(f"Plan {cfg.plan_id}: {len(cards)} open cards", file=err)
    print(f"Buckets: {', '.join(cfg.buckets)}", file=err)
    print(f"Labels: {m.prefix_by_category or 'none matched config prefixes'}", file=err)
    names = sorted(u["displayName"] for u in m.user_by_id.values() if u.get("displayName"))
    print(f"People known: {names}", file=err)
    if m.unknown_user_ids:
        print(
            f"Assignee ids on the board with no name yet (add to config `people`): "
            f"{sorted(m.unknown_user_ids)}",
            file=err,
        )
    print("OK. Add the server to Claude desktop; see README.", file=err)
    return 0


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] in ("login", "check"):
        try:
            sys.exit(login())
        except (cfgmod.ConfigError, GraphError, AuthError, RuntimeError) as e:
            print(f"planner-mcp: {e}", file=sys.stderr)
            sys.exit(1)
    if sys.stdin.isatty():
        print(
            "planner-mcp speaks MCP over stdio and is meant to be launched by Claude desktop.\n"
            "For first-time sign-in and a connectivity check, run:  planner-mcp login",
            file=sys.stderr,
        )
        sys.exit(2)
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
