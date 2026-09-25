import subprocess

import pytest

from planner_mcp import github as gh
from planner_mcp.config import GitHubConfig

from .conftest import BUG_FORM
from .fake_gh import FakeGh


def test_parse_form_reads_sections_and_no_response():
    fields = gh.parse_form(BUG_FORM.format(sev="S2 - wrong behavior").replace("\n", "\r\n"))
    assert fields["what_happened"] == "POST /ask/ with a trailing slash returns 500."
    assert fields["steps_to_reproduce"].startswith("1. curl")
    assert fields["environment"] == "dev"
    assert fields["link_or_commit"] is None  # "_No response_"
    assert fields["severity"] == "S2 - wrong behavior"


def test_parse_form_unwraps_rendered_fields():
    body = ("### Environment\n\n_No response_\n\n### Logs or request ID\n\n"
            "```text\n403 Forbidden\nrequest_id=7f3a\n```\n")
    assert gh.parse_form(body) == {
        "environment": None, "logs_or_request_id": "403 Forbidden\nrequest_id=7f3a"}


def test_parse_form_on_a_plain_body():
    assert gh.parse_form("It answers in Spanish sometimes.") == {}
    assert gh.parse_form(None) == {}


@pytest.mark.parametrize("labels,form,want", [
    (["bug"], "S3 - cosmetic", "S3"),
    (["bug", "S1"], "S3 - cosmetic", "S1"),  # triage label beats the form
    (["severity:S2"], None, "S2"),
    (["sev-1"], None, "S1"),
    (["bug", "S12"], None, None),
    (["bug"], None, None),
])
def test_severity(labels, form, want):
    assert gh.severity(labels, {"severity": form}) == want


def _client(fake, **kw) -> gh.GitHubClient:
    return gh.GitHubClient(GitHubConfig(repos={"NIM": "FO-AI/nimbus"}, **kw), runner=fake)


@pytest.mark.parametrize("mode,msg", [
    ("missing", "gh CLI not found"),
    ("timeout", "timed out after 20s on FO-AI/nimbus"),
    ("logged_out", "gh auth login"),
    ("sso", "SSO"),
])
def test_gh_failures_become_clear_messages(mode, msg):
    fake = FakeGh()
    fake.add_repo("FO-AI/nimbus")
    fake.mode = mode
    with pytest.raises(gh.GitHubError, match=msg):
        _client(fake).list_issues("FO-AI/nimbus", "bug")


def test_unknown_repo_and_issue_messages():
    fake = FakeGh()
    fake.add_repo("FO-AI/nimbus")
    c = _client(fake)
    with pytest.raises(gh.GitHubError, match="Repo FO-AI/gone not found"):
        c.list_issues("FO-AI/gone", "bug")
    with pytest.raises(gh.GitHubError, match="FO-AI/nimbus#9 not found"):
        c.view_issue("FO-AI/nimbus", 9)


def test_auth_problem():
    fake = FakeGh()
    assert _client(fake).auth_problem() is None
    fake.mode = "logged_out"
    assert "gh auth login" in _client(fake).auth_problem()


def test_subprocess_runner_never_touches_the_mcp_wire(monkeypatch):
    seen = {}

    def fake_run(argv, **kw):
        seen.update(kw, argv=argv)
        return subprocess.CompletedProcess(argv, 0, "[]", "")

    monkeypatch.setattr(gh.subprocess, "run", fake_run)
    runner = gh.SubprocessRunner()
    runner.path = "/opt/homebrew/bin/gh"
    assert runner(["issue", "list"], 7) == (0, "[]", "")
    assert seen["argv"] == ["/opt/homebrew/bin/gh", "issue", "list"]
    assert seen["stdin"] is subprocess.DEVNULL  # stdin is the MCP transport
    assert seen["capture_output"] is True  # so is stdout
    assert seen["timeout"] == 7
    assert seen["env"]["GH_PROMPT_DISABLED"] == "1"

    runner.path = None
    with pytest.raises(FileNotFoundError):
        runner(["auth", "status"], 7)


def test_find_gh_order(monkeypatch):
    exists = set()
    monkeypatch.setattr(gh.os.path, "exists", lambda p: p in exists)
    monkeypatch.setattr(gh.shutil, "which", lambda name: None)
    assert gh.find_gh() is None
    exists.add("/usr/local/bin/gh")
    assert gh.find_gh() == "/usr/local/bin/gh"
    exists.add("/opt/homebrew/bin/gh")
    assert gh.find_gh() == "/opt/homebrew/bin/gh"  # Homebrew on Apple silicon first
    monkeypatch.setattr(gh.shutil, "which", lambda name: "/somewhere/gh")
    assert gh.find_gh() == "/somewhere/gh"  # PATH beats the fallbacks
    exists.add("/custom/gh")
    assert gh.find_gh("/custom/gh") == "/custom/gh"  # config beats everything
    assert gh.find_gh("/not/there/gh") is None  # a wrong config path is not silently ignored
