"""The MCP layer: tools are registered, and errors come back as data, not exceptions."""

import asyncio

import pytest

from planner_mcp import server


@pytest.fixture(autouse=True)
def wire_board(board, monkeypatch):
    monkeypatch.setattr(server, "_board", board)


def test_tools_are_registered():
    names = {t.name for t in asyncio.run(server.mcp.list_tools())}
    assert names == {
        "board_snapshot", "list_tasks", "get_task", "board_info",
        "create_task", "move_task", "update_task", "set_checklist", "add_note", "set_description",
    }


def test_snapshot_tool_roundtrip():
    snap = server.board_snapshot()
    assert "This Week" in snap["buckets"]
    assert set(snap["people"]) >= {"Chris Kim", "Daniel Zhang", "Om Shewale"}


def test_list_tasks_filters():
    assert len(server.list_tasks(bucket="This Week")) == 1
    assert len(server.list_tasks(assignee="chris")) == 1
    assert len(server.list_tasks(label="[PIQ]")) == 1
    assert server.list_tasks(label="OPS") == []  # only OPS card is completed


def test_convention_error_is_returned_not_raised():
    out = server.create_task("no prefix here")
    assert out["error"] == "convention"
    assert "prefix" in out["message"]


def test_graph_error_is_returned_not_raised():
    out = server.get_task("t-does-not-exist")
    assert out["error"] == "graph" and out["status"] == 404


def test_create_via_tool_lands_in_backlog(fake):
    out = server.create_task("[QUAD] Input gate ported from position-iq", tier="Medium",
                             checklist=["Copy gate module", "Wire to get-token"])
    assert out["bucket"] == "Backlog" and out["labels"] == ["QUAD"]
    assert fake.tasks[out["id"]]["priority"] == 3
