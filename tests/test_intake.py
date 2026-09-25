"""Bug intake end to end on fakes: fake gh for GitHub, fake Graph for Planner."""

import pytest

from planner_mcp import conventions as conv
from planner_mcp.github import GitHubError

PIQ19 = "https://github.com/FO-AI/position-iq/issues/19"


def _details_gets(fake):
    return [p for m, p in fake.calls if m == "GET" and p.startswith("/planner/tasks/")
            and p.endswith("/details")]


def test_lists_open_bugs_only_sorted_by_severity_then_age(intake):
    out = intake.open_issues()
    assert [i["issue"] for i in out["issues"]] == [
        "FO-AI/position-iq#19",  # S1
        "FO-AI/nimbus#42",  # S2
        "FO-AI/nimbus#43",  # S3
        "FO-AI/benny#5",  # no severity
    ]  # type Bug, open; not the Feature labelled bug, the Task, or the closed one
    piq = out["issues"][0]
    assert piq["severity"] == "S1" and piq["suggested_prefix"] == "PIQ"
    assert piq["age_days"] == 3 and piq["author"] == "eshaam"
    assert piq["url"] == PIQ19 and piq["linked_task_id"] is None
    assert piq["fields"]["what_happened"].startswith("POST /ask/")
    assert piq["type"] == "Bug" and piq["issue_fields"] == {"Product": "PIQ", "Area": "API"}
    assert "body" not in piq  # form issues carry fields, not the raw body
    benny = out["issues"][-1]
    assert benny["fields"] == {} and benny["body"] == "It answers in Spanish sometimes."
    assert out["types"] == ["Bug"] and out["linked_done"] == [] and out["hidden_linked"] == 0
    assert benny["labels"] == []  # found by type alone; no bug label needed


def test_org_priority_sorts_first(intake, gh_fake):
    gh_fake.issue("FO-AI/benny", 5)["fields"] = {"Priority": "Urgent"}
    gh_fake.issue("FO-AI/nimbus", 43)["fields"] = {"Priority": "Low"}
    order = [i["issue"] for i in intake.open_issues()["issues"]]
    assert order == [
        "FO-AI/benny#5",  # Urgent
        "FO-AI/nimbus#43",  # Low still beats no priority
        "FO-AI/position-iq#19",  # then S1/S2 labels or form, then age
        "FO-AI/nimbus#42",
    ]


def test_unreadable_field_catalogue_does_not_break_the_list(intake, gh_fake):
    gh_fake.fields_broken = True
    gh_fake.issue("FO-AI/benny", 5)["fields"] = {"Priority": "Urgent"}
    out = intake.open_issues()
    assert len(out["issues"]) == 4 and "repo_errors" not in out
    assert out["issues"][-1]["issue_fields"] == {"Priority": "Urgent"}  # shown, just not ranked


def test_project_filter(intake):
    assert [i["issue"] for i in intake.open_issues("PIQ")["issues"]] == ["FO-AI/position-iq#19"]
    assert len(intake.open_issues("[NIM]")["issues"]) == 2
    assert len(intake.open_issues("nimbus")["issues"]) == 2
    with pytest.raises(conv.ConventionError, match="Unknown project"):
        intake.open_issues("OPS")


def test_one_call_per_issue_type(intake, gh_fake, cfg):
    cfg.github.intake_types = ["Bug", "Task"]
    gh_fake.add_issue("FO-AI/nimbus", 51, "Rotate the dev key", labels=(), type="Task")
    out = intake.open_issues("NIM")
    assert sorted(i["number"] for i in out["issues"]) == [42, 43, 51]
    assert out["types"] == ["Bug", "Task"]
    assert sorted(v["type"] for v in gh_fake.graphql_calls("IntakeList")) == ["Bug", "Task"]


def test_create_with_issue_links_card_and_hides_issue(intake, board, fake):
    """The acceptance scenario: create a card for an issue; the next intake_list hides it and
    a second card for the same issue is refused."""
    ref = intake.resolve("FO-AI/position-iq#19")
    before = len([p for m, p in fake.calls if m == "PATCH"])
    card = board.create("[PIQ] Sample PD data indexed in the Fabric environment",
                        bucket="Shaping", assignee="Daniel", description="Repro: see issue.",
                        checklist=["Load sample", "Verify index"], issue=ref)
    assert card["issues"] == ["FO-AI/position-iq#19"]
    assert card["description"] == "Issue: FO-AI/position-iq#19\n\nRepro: see issue."
    assert card["bucket"] == "Shaping" and len(card["checklist"]) == 2
    assert "warnings" not in card
    patches = [p for m, p in fake.calls if m == "PATCH"][before:]
    assert patches == [f"/planner/tasks/{card['id']}/details"]  # one details write
    stored = fake.details[card["id"]]["references"]
    assert list(stored) == ["https%3A//github%2Ecom/FO-AI/position-iq/issues/19"]
    assert fake.tasks[card["id"]]["referenceCount"] == 1

    out = intake.open_issues("PIQ")
    assert out["issues"] == [] and out["hidden_linked"] == 1
    shown = intake.open_issues("PIQ", include_linked=True)["issues"]
    assert shown[0]["linked_task_id"] == card["id"]

    with pytest.raises(conv.DuplicateIssue) as e:
        board.create("[PIQ] Something else entirely", issue=intake.resolve("PIQ#19"))
    assert e.value.task_id == card["id"]


def test_issue_on_a_completed_card_is_reported_not_offered(intake, board, fake):
    tid = fake.add_task("[PIQ] Old attempt", "Review", percent=100,
                        references={PIQ19: "FO-AI/position-iq#19"})
    out = intake.open_issues("PIQ")
    assert out["issues"] == []
    assert out["linked_done"] == [{
        "issue": "FO-AI/position-iq#19", "title": "PD data missing in Fabric env",
        "url": PIQ19, "age_days": 3, "linked_task_id": tid,
    }]
    with pytest.raises(conv.DuplicateIssue, match="completed"):
        board.create("[PIQ] Try again", issue=intake.resolve("PIQ#19"))


def test_only_cards_with_references_are_read(intake, fake):
    fake.add_task("[NIM] Has a doc", references={
        "https://adminliveunc.sharepoint.com/sites/x/Plan.docx": "Plan"})
    fake.add_task("[NIM] Tracks 42", references={
        "https://github.com/FO-AI/nimbus/issues/42": None})  # pasted in the Planner UI: no alias
    fake.calls.clear()
    out = intake.open_issues("NIM")
    assert len(_details_gets(fake)) == 2  # of 6 tasks, only the two with references
    assert [i["number"] for i in out["issues"]] == [43] and out["hidden_linked"] == 1


def test_renamed_repo_still_dedups(intake, fake, gh_fake, cfg):
    fake.add_task("[QUAD] Language fix", references={
        "https://github.com/FO-AI/benny/issues/5": "FO-AI/benny#5"})
    gh_fake.url_name["FO-AI/benny"] = "FO-AI/quad"  # repo renamed; gh reports new URLs
    assert [i["number"] for i in intake.open_issues("QUAD")["issues"]] == [5]  # would duplicate
    cfg.github.renamed = {"FO-AI/benny": "FO-AI/quad"}
    assert intake.open_issues("QUAD")["issues"] == []


def test_resolve_refuses_prs_and_closed_issues(intake):
    with pytest.raises(conv.ConventionError, match="pull request"):
        intake.resolve("FO-AI/position-iq#33")
    with pytest.raises(conv.ConventionError, match="closed"):
        intake.resolve("NIM#40")
    assert intake.resolve("NIM#40", require_open=False).number == 40
    with pytest.raises(GitHubError, match="not found"):
        intake.resolve("NIM#999")


def test_title_prefix_that_disagrees_with_the_repo_warns(intake, board):
    card = board.create("[PIQ] Trailing slash handled on /ask", issue=intake.resolve("NIM#42"))
    assert card["warnings"] == ["FO-AI/nimbus is [NIM] work but the title is [PIQ]."]


def test_link_issue_to_an_existing_card(intake, board, fake):
    tid = next(t for t in fake.tasks.values() if t["title"].startswith("[NIM]"))["id"]
    card = board.link_issue(tid, intake.resolve("NIM#42"))
    assert card["issues"] == ["FO-AI/nimbus#42"]
    assert card["description"].startswith("Issue: FO-AI/nimbus#42\n\nAC: /ask answers")
    assert len(card["checklist"]) == 2  # untouched

    patches = sum(1 for m, _ in fake.calls if m == "PATCH")
    again = board.link_issue(tid, intake.resolve("FO-AI/nimbus#42"))
    assert again["description"] == card["description"]
    assert sum(1 for m, _ in fake.calls if m == "PATCH") == patches  # no-op

    other = next(t for t in fake.tasks.values() if t["title"].startswith("[QUAD]"))["id"]
    with pytest.raises(conv.DuplicateIssue):
        board.link_issue(other, intake.resolve("NIM#42"))


def test_one_repo_failing_still_returns_the_others(intake, gh_fake):
    gh_fake.broken.add("FO-AI/benny")
    out = intake.open_issues()
    assert {i["repo"] for i in out["issues"]} == {"FO-AI/nimbus", "FO-AI/position-iq"}
    assert out["repo_errors"][0]["repo"] == "FO-AI/benny"
    assert "not found" in out["repo_errors"][0]["message"]


def test_every_repo_failing_is_an_error(intake, gh_fake):
    gh_fake.mode = "logged_out"
    with pytest.raises(GitHubError, match="gh auth login"):
        intake.open_issues()


def test_long_free_form_body_is_capped(intake, gh_fake):
    gh_fake.add_issue("FO-AI/benny", 6, "Long one", body="x" * 5000)
    body = next(i for i in intake.open_issues("QUAD")["issues"] if i["number"] == 6)["body"]
    assert len(body) == 1501 and body.endswith("…")
    assert len(intake.detail("QUAD#6")["body"]) == 5000  # intake_item has it whole


def test_limit_hit_is_flagged(intake, cfg):
    cfg.github.limit_per_repo = 1
    assert intake.open_issues("NIM")["truncated"] == ["FO-AI/nimbus:Bug"]


def test_intake_item_has_everything(intake):
    item = intake.detail("https://github.com/FO-AI/nimbus/issues/42")
    assert item["issue"] == "FO-AI/nimbus#42" and item["state"] == "open"
    assert item["body"].startswith("### What happened")
    assert item["fields"]["severity"].startswith("S2") and item["severity"] == "S2"
    assert item["comments"] == [
        {"author": "chriskim", "created": "2026-09-20", "body": "Also happens on /search/."}]
    assert item["linked_task_id"] is None and item["linked_task_completed"] is None
    with pytest.raises(conv.ConventionError, match="pull request"):
        intake.detail("FO-AI/position-iq#33")


@pytest.mark.parametrize("text", ["#19", "19", " #19 "])
def test_bare_number_is_looked_up_across_repos(intake, text):
    assert intake.detail(text)["issue"] == "FO-AI/position-iq#19"


def test_bare_number_prefers_the_one_open_issue(intake, gh_fake):
    gh_fake.add_issue("FO-AI/nimbus", 19, "Old nimbus bug", state="CLOSED")
    assert intake.detail("#19")["issue"] == "FO-AI/position-iq#19"
    assert intake.resolve("#19").repo == "FO-AI/position-iq"  # create_task / link_issue too


def test_bare_number_ignores_prs_and_does_not_guess_past_an_unreachable_repo(intake, gh_fake):
    with pytest.raises(conv.ConventionError, match="No issue #33"):
        intake.detail("#33")  # position-iq#33 is a PR
    gh_fake.broken.add("FO-AI/benny")  # benny might have a #19 we can't see
    with pytest.raises(GitHubError, match="FO-AI/benny"):
        intake.detail("#19")
