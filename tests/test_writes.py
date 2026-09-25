"""The two GitHub writes: close_issue and set_issue_fields. Fake gh + fake Graph."""

import pytest

from planner_mcp import conventions as conv
from planner_mcp.graph import GraphError

PIQ19 = "https://github.com/FO-AI/position-iq/issues/19"


def _closes(gh_fake):
    return [c for c in gh_fake.calls if c[:2] == ["issue", "close"]]


# ---- close_issue -----------------------------------------------------------------


def test_close_an_unlinked_issue(intake, gh_fake):
    out = intake.close("PIQ#19")
    assert out == {
        "issue": "FO-AI/position-iq#19", "url": PIQ19, "title": "PD data missing in Fabric env",
        "closed": True, "already_closed": False, "reason": "completed", "cards_ticked": [],
    }
    issue = gh_fake.issue("FO-AI/position-iq", 19)
    assert issue["state"] == "CLOSED" and issue["closed_reason"] == "completed"
    assert intake.open_issues("PIQ")["issues"] == []


def test_close_as_not_planned_with_a_comment(intake, gh_fake):
    intake.close("NIM#43", reason="not_planned", comment="Copy is changing anyway.")
    issue = gh_fake.issue("FO-AI/nimbus", 43)
    assert issue["closed_reason"] == "not planned"
    assert issue["comments"][-1]["body"] == "Copy is changing anyway."


def test_close_ticks_the_open_card_only(intake, fake):
    open_card = fake.add_task("[PIQ] PD data indexed", "In Progress", references={PIQ19: None})
    done_card = fake.add_task("[PIQ] Earlier attempt", "Review", percent=100,
                              references={PIQ19: None})
    out = intake.close("FO-AI/position-iq#19")
    assert [c["id"] for c in out["cards_ticked"]] == [open_card]
    assert out["cards_ticked"][0]["bucket"] == "In Progress"
    assert fake.tasks[open_card]["percentComplete"] == 100
    assert fake.tasks[done_card]["percentComplete"] == 100  # untouched, already done


def test_already_closed_issue_is_not_closed_again_but_card_is_ticked(intake, gh_fake, fake):
    tid = fake.add_task("[NIM] Old crash handled", "Review",
                        references={"https://github.com/FO-AI/nimbus/issues/40": None})
    out = intake.close("NIM#40")
    assert out["already_closed"] is True and out["reason"] is None
    assert _closes(gh_fake) == []
    assert fake.tasks[tid]["percentComplete"] == 100


def test_close_refuses_prs_bad_reasons_and_bare_numbers(intake, gh_fake):
    with pytest.raises(conv.ConventionError, match="pull request"):
        intake.close("FO-AI/position-iq#33")
    with pytest.raises(conv.ConventionError, match="reason must be"):
        intake.close("PIQ#19", reason="duplicate")
    with pytest.raises(conv.ConventionError, match="No issue #36 in FO-AI/nimbus"):
        intake.close("#36")  # no repo has an issue #36
    assert _closes(gh_fake) == []


def test_close_by_bare_number_finds_the_repo(intake, gh_fake):
    out = intake.close("#19")  # only position-iq has an issue #19
    assert out["issue"] == "FO-AI/position-iq#19" and out["already_closed"] is False
    assert gh_fake.issue("FO-AI/position-iq", 19)["state"] == "CLOSED"


def test_close_by_bare_number_refuses_when_two_open_issues_share_it(intake, gh_fake):
    gh_fake.add_issue("FO-AI/nimbus", 19, "Login loop on Safari")
    with pytest.raises(conv.ConventionError, match="more than one repo") as e:
        intake.close("#19")
    assert "FO-AI/nimbus#19 'Login loop on Safari' (open)" in str(e.value)
    assert "FO-AI/position-iq#19" in str(e.value)
    assert _closes(gh_fake) == []


def test_planner_failure_after_close_is_reported_not_hidden(intake, board, fake, monkeypatch):
    fake.add_task("[PIQ] PD data indexed", "In Progress", references={PIQ19: None})

    def down(*a, **kw):
        raise GraphError(503, "Service unavailable")

    monkeypatch.setattr(board, "update", down)
    out = intake.close("PIQ#19")
    assert out["closed"] is True and out["cards_ticked"] == []
    assert "Issue closed, but the card could not be ticked" in out["card_error"]


# ---- set_issue_fields ------------------------------------------------------------


def test_set_fields_upserts_only_what_is_named(intake, gh_fake):
    out = intake.set_fields("PIQ#19", {"priority": "high", "AREA": "ai / llm"})
    assert out["issue_fields"] == {"Product": "PIQ", "Area": "AI / LLM", "Priority": "High"}
    assert out["type"] == "Bug" and out["issue"] == "FO-AI/position-iq#19"


def test_set_a_date_and_clear_a_field(intake, gh_fake):
    intake.set_fields("PIQ#19", {"Target date": "2026-10-02", "Area": ""})
    assert gh_fake.issue("FO-AI/position-iq", 19)["fields"] == {
        "Product": "PIQ", "Target date": "2026-10-02"}


@pytest.mark.parametrize("fields,msg", [
    ({"Severity": "S1"}, "No issue field 'Severity'"),
    ({"Priority": "Critical"}, "Options: Urgent, High, Medium, Low"),
    ({"Target date": "next Friday"}, "YYYY-MM-DD"),
    ({}, "Nothing to set"),
])
def test_set_fields_refuses_bad_input_without_writing(intake, gh_fake, fields, msg):
    with pytest.raises(conv.ConventionError, match=msg):
        intake.set_fields("PIQ#19", fields)
    assert gh_fake.graphql_calls("SetIssueFields") == []


def test_new_option_is_found_after_one_catalogue_refresh(intake, gh_fake):
    intake.set_fields("PIQ#19", {"Priority": "Low"})  # caches the catalogue
    area = next(f for f in gh_fake.org_fields if f["name"] == "Area")
    area["options"].append({"id": "O_data", "name": "Data pipeline"})
    intake.set_fields("PIQ#19", {"Area": "Data pipeline"})
    assert gh_fake.issue("FO-AI/position-iq", 19)["fields"]["Area"] == "Data pipeline"
    assert len(gh_fake.graphql_calls("OrgFields")) == 2


def test_set_fields_refuses_a_pr(intake, gh_fake):
    with pytest.raises(conv.ConventionError, match="pull request"):
        intake.set_fields("FO-AI/position-iq#33", {"Priority": "High"})
