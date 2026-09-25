import pytest

from planner_mcp import conventions as conv


def test_snapshot_groups_by_bucket_and_hides_completed(board):
    snap = board.snapshot()
    assert list(snap["buckets"]) == board.cfg.buckets
    assert len(snap["buckets"]["This Week"]) == 1
    assert snap["buckets"]["Review"] == []  # the 100% card is hidden
    card = snap["buckets"]["This Week"][0]
    assert card["assignee"] == "Chris Kim"
    assert card["labels"] == ["NIM"]
    assert card["tier"] == "Medium"
    assert card["due"] == "2026-09-26"
    assert card["checklist_total"] == 2 and card["checklist_open"] == 2
    assert snap["load_by_person"]["Chris Kim"] == {"This Week": 1}
    assert "(unassigned)" in snap["load_by_person"]


def test_snapshot_can_include_completed(board):
    snap = board.snapshot(include_completed=True)
    assert len(snap["buckets"]["Review"]) == 1
    assert snap["buckets"]["Review"][0]["completed"] is True


def test_get_card_has_description_and_checklist(board, fake):
    tid = next(t for t in fake.tasks.values() if t["title"].startswith("[NIM]"))["id"]
    c = board.card(tid)
    assert c["description"].startswith("AC:")
    assert [i["title"] for i in c["checklist"]] == ["Pin SDK", "Smoke test"]
    assert c["details_etag"]


def test_create_sets_label_tier_assignee_and_details(board, fake):
    c = board.create(
        "[OPS] Claude reads the live Planner board through a local MCP server",
        bucket="Shaping",
        tier="Medium",
        assignee="Kaiji",
        due="2026-10-03",
        description="Why: no more Excel exports.",
        checklist=["Register app", "Device flow", "board_snapshot tool"],
    )
    assert c["bucket"] == "Shaping"
    assert c["labels"] == ["OPS"]
    assert c["priority"] == 3 and c["tier"] == "Medium"
    assert c["assignee"] == "Kaiji Fu"
    assert c["due"] == "2026-10-03"
    assert c["description"] == "Why: no more Excel exports."
    assert len(c["checklist"]) == 3
    raw = fake.tasks[c["id"]]
    assert raw["appliedCategories"] == {"category4": True}


def test_create_rejects_bad_prefix_and_long_checklist(board):
    with pytest.raises(conv.ConventionError):
        board.create("[BEN] Something", "Backlog")
    with pytest.raises(conv.ConventionError):
        board.create("Work on data mapping", "Backlog")
    with pytest.raises(conv.ConventionError):
        board.create("[NIM] Fine title", "Backlog", checklist=[f"s{i}" for i in range(21)])


def test_move_and_update_use_etags(board, fake):
    tid = next(t for t in fake.tasks.values() if t["title"].startswith("[PIQ]"))["id"]
    c = board.move(tid, "shaping")  # case-insensitive bucket
    assert c["bucket"] == "Shaping"
    c = board.update(tid, assignee="Daniel", due="2026-09-30", tier="Large")
    assert c["assignee"] == "Daniel Zhang" and c["extra_assignees"] == []
    assert c["due"] == "2026-09-30" and c["tier"] == "Large"
    # replacing the assignee removes the old one
    c = board.update(tid, assignee="Chris")
    assert c["assignee"] == "Chris Kim" and c["extra_assignees"] == []
    c = board.update(tid, percent_complete=100)
    assert c["completed"] is True
    patches = [p for m, p in fake.calls if m == "PATCH"]
    assert patches and all("/planner/tasks/" in p for p in patches)


def test_412_is_retried_once_with_fresh_etag(board, fake):
    tid = next(t for t in fake.tasks.values() if t["title"].startswith("[PIQ]"))["id"]
    fake.force_412_once = True
    c = board.move(tid, "Blocked")
    assert c["bucket"] == "Blocked"
    assert sum(1 for m, p in fake.calls if m == "PATCH" and p.endswith(tid)) == 2


def test_set_checklist_replace_and_append(board, fake):
    tid = next(t for t in fake.tasks.values() if t["title"].startswith("[NIM]"))["id"]
    c = board.set_checklist(tid, ["A", "B", "C"])
    assert [i["title"] for i in c["checklist"]] == ["A", "B", "C"]
    c = board.set_checklist(tid, ["D"], replace=False)
    assert len(c["checklist"]) == 4
    with pytest.raises(conv.ConventionError):
        board.set_checklist(tid, [f"x{i}" for i in range(17)], replace=False)


def test_add_note_appends_dated_line(board, fake):
    tid = next(t for t in fake.tasks.values() if t["title"].startswith("[NIM]"))["id"]
    c = board.add_note(tid, "Carried back: waiting on SDK pin from Om.")
    lines = c["description"].splitlines()
    assert lines[0].startswith("AC:")
    assert lines[-1].startswith("[20") and lines[-1].endswith("waiting on SDK pin from Om.")


def test_ambiguous_or_unknown_assignee(board):
    with pytest.raises(conv.ConventionError):
        board.resolve_user("nobody")
    assert board.resolve_user("Chris") == "u-chris"
    assert board.resolve_user("me") == "u-om"
    with pytest.raises(conv.ConventionError):
        board.resolve_user("a")  # matches several


def test_unknown_assignee_resolved_by_lookup_then_cached(board, fake):
    # Kaiji is not in config `people`; the OPS card is his. Lookup is on.
    board.cards(include_completed=True)
    assert fake.user_lookups == 1
    board.cards(include_completed=True)
    assert fake.user_lookups == 1  # cached
    assert board.meta().user_by_id["u-kaiji"]["displayName"] == "Kaiji Fu"
    assert board.resolve_user("Kaiji") == "u-kaiji"


def test_unknown_assignee_without_permission_falls_back_to_id(cfg, fake):
    from planner_mcp.board import Board
    from planner_mcp.graph import PlannerClient

    fake.allow_user_lookup = False
    b = Board(cfg, PlannerClient("PLAN1", token=lambda: "T", transport=fake.transport()))
    cards = b.cards(include_completed=True)
    ops = next(c for c in cards if c["title"].startswith("[OPS]"))
    assert ops["assignee"] == "u-kaiji"  # raw id, not a crash
    assert "u-kaiji" in b.meta().unknown_user_ids
    assert fake.user_lookups == 1  # 403 remembered, not retried per card


def test_bucket_names_match_case_insensitively(cfg, fake):
    from planner_mcp.board import Board
    from planner_mcp.graph import PlannerClient

    for b in fake.buckets.values():
        if b["name"] == "This Week":
            b["name"] = "This week"  # how Planner actually has it
    b = Board(cfg, PlannerClient("PLAN1", token=lambda: "T", transport=fake.transport()))
    snap = b.snapshot()
    assert len(snap["buckets"]["This Week"]) == 1  # config spelling wins in output
    tid = snap["buckets"]["Backlog"][0]["id"]
    assert b.move(tid, "this WEEK")["bucket"] == "This Week"
