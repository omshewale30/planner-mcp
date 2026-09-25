import pytest

from planner_mcp import links
from planner_mcp.conventions import ConventionError

from .fake_graph import encode_key as graph_encode

REPOS = {"NIM": "FO-AI/nimbus", "PIQ": "FO-AI/position-iq", "QUAD": "FO-AI/benny"}


@pytest.mark.parametrize("text", [
    "FO-AI/nimbus#42",
    "fo-ai/NIMBUS#42",
    "https://github.com/FO-AI/nimbus/issues/42",
    "https://github.com/FO-AI/nimbus/issues/42#issuecomment-99",
    "https://github.com/FO-AI/nimbus/issues/42?q=1",
    "nimbus#42",
    "NIM#42",
    "  nim#42 ",
])
def test_parse_ref_accepts_the_usual_spellings(text):
    ref = links.parse_ref(text, REPOS)
    assert (ref.repo, ref.number) == ("FO-AI/nimbus", 42)  # configured spelling wins
    assert str(ref) == "FO-AI/nimbus#42"


@pytest.mark.parametrize("text,msg", [
    ("https://github.com/FO-AI/position-iq/pull/33", "pull request"),
    ("FO-AI/other#1", "not a configured repo"),
    ("nimbus 42", "Can't read issue"),
    ("", "Can't read issue"),
])
def test_parse_ref_rejects(text, msg):
    with pytest.raises(ConventionError, match=msg):
        links.parse_ref(text, REPOS)


def test_reference_key_encoding_matches_graph_and_round_trips():
    url = "https://github.com/FO-AI/nimbus/issues/42"
    key = links.encode_key(url)
    assert key == "https%3A//github%2Ecom/FO-AI/nimbus/issues/42" == graph_encode(url)
    assert links.decode_key(key) == url
    body = links.reference_body(links.IssueRef("FO-AI/nimbus", 42))
    assert body[key]["alias"] == "FO-AI/nimbus#42"
    assert body[key]["@odata.type"] == "#microsoft.graph.plannerExternalReference"


def test_only_github_issue_urls_count_as_links():
    assert links.issue_in_url("https://github.com/FO-AI/nimbus/issues/42") == ("FO-AI/nimbus", 42)
    assert links.issue_in_url("https://github.com/FO-AI/nimbus/pull/42") is None
    assert links.issue_in_url("https://adminliveunc.sharepoint.com/x/Plan.docx") is None


def test_link_key_is_case_insensitive_and_follows_renames():
    assert links.link_key("FO-AI/Nimbus", 1) == links.link_key("fo-ai/nimbus", 1)
    renamed = {"FO-AI/benny": "FO-AI/quad"}
    assert links.link_key("FO-AI/benny", 5, renamed) == links.link_key("FO-AI/quad", 5, renamed)
