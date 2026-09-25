"""The MCP layer: tools are registered, and errors come back as data, not exceptions."""

import asyncio

import pytest

from planner_mcp import server
from planner_mcp.github import GitHubClient


@pytest.fixture(autouse=True)
def wire_board(board, intake, monkeypatch):
    monkeypatch.setattr(server, "_board", board)
    monkeypatch.setattr(server, "_intake", intake)


def test_tools_are_registered():
    names = {t.name for t in asyncio.run(server.mcp.list_tools())}
    assert names == {
        "board_snapshot", "list_tasks", "get_task", "board_info",
        "create_task", "move_task", "update_task", "set_checklist", "add_note", "set_description",
        "intake_list", "intake_item", "link_issue", "close_issue", "set_issue_fields",
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


def test_intake_list_tool_roundtrip():
    out = server.intake_list(project="PIQ")
    assert [i["issue"] for i in out["issues"]] == ["FO-AI/position-iq#19"]


def test_create_task_with_issue_then_duplicate_is_refused_as_data(fake):
    out = server.create_task("[PIQ] Sample PD data indexed in the Fabric environment",
                             bucket="Shaping", issue="FO-AI/position-iq#19")
    assert out["issues"] == ["FO-AI/position-iq#19"]
    assert server.intake_list(project="PIQ")["issues"] == []
    dup = server.create_task("[PIQ] Another card", issue="PIQ#19")
    assert dup["error"] == "convention" and dup["task_id"] == out["id"]


@pytest.mark.parametrize("mode", ["missing", "timeout", "logged_out", "sso"])
def test_github_failures_are_returned_not_raised(gh_fake, fake, mode):
    gh_fake.mode = mode
    before = len(fake.tasks)
    assert server.intake_list()["error"] == "github"
    assert server.intake_item("NIM#42")["error"] == "github"
    out = server.create_task("[NIM] Trailing slash handled", issue="NIM#42")
    assert out["error"] == "github"
    assert len(fake.tasks) == before  # no card when GitHub can't confirm the issue


def test_link_issue_tool_and_bad_ref():
    tid = server.list_tasks(label="QUAD")[0]["id"]
    assert server.link_issue(tid, "QUAD#5")["issues"] == ["FO-AI/benny#5"]
    assert server.link_issue(tid, "not an issue")["error"] == "convention"


def test_no_github_section_means_config_error(board, monkeypatch):
    monkeypatch.setattr(board.cfg, "github", None)
    monkeypatch.setattr(server, "_intake", None)
    assert server.intake_list()["error"] == "config"
    assert server.create_task("[NIM] Plain card still works")["labels"] == ["NIM"]


def test_write_tools_roundtrip(gh_fake):
    out = server.set_issue_fields("PIQ#19", {"Priority": "High"})
    assert out["issue_fields"] == {"Product": "PIQ", "Area": "API", "Priority": "High"}
    out = server.close_issue("PIQ#19")
    assert out["closed"] is True and gh_fake.issue("FO-AI/position-iq", 19)["state"] == "CLOSED"
    again = server.close_issue("#19")  # bare number: only position-iq has #19
    assert again["issue"] == "FO-AI/position-iq#19" and again["already_closed"] is True
    gh_fake.mode = "sso"
    assert server.set_issue_fields("NIM#42", {"Priority": "Low"})["error"] == "github"


def test_login_github_check_reports_each_repo(cfg, gh_fake, monkeypatch):
    import io

    monkeypatch.setattr(server, "GitHubClient", lambda c: GitHubClient(c, runner=gh_fake))
    gh_fake.broken.add("FO-AI/benny")
    err = io.StringIO()
    server._github_check(cfg, err)
    lines = err.getvalue().splitlines()
    assert lines[0] == "GitHub: gh at /fake/bin/gh"
    assert "[NIM] FO-AI/nimbus: reachable, 2 open Bug issue(s)" in lines[1]
    assert "[PIQ] FO-AI/position-iq: reachable, 1 open Bug issue(s)" in lines[2]
    assert "[QUAD] FO-AI/benny: Repo FO-AI/benny not found" in lines[3]
