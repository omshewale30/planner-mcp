import pytest
import yaml

from planner_mcp import config

BASE = {
    "tenant_id": "t", "client_id": "c", "plan_id": "p",
    "buckets": ["Backlog"], "prefixes": ["NIM", "PIQ", "QUAD", "OPS"],
}


def _load(tmp_path, **extra):
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump({**BASE, **extra}))
    return config.load(path)


def test_github_section_is_optional(tmp_path):
    assert _load(tmp_path).github is None


def test_github_section_parses_with_defaults(tmp_path):
    g = _load(tmp_path, github={"repos": {"nim": "FO-AI/nimbus", "QUAD": "FO-AI/benny"}}).github
    assert g.repos == {"NIM": "FO-AI/nimbus", "QUAD": "FO-AI/benny"}
    assert g.intake_types == ["Bug"] and g.limit_per_repo == 50 and g.gh_path is None
    assert g.prefix_for("fo-ai/BENNY") == "QUAD" and g.prefix_for("FO-AI/other") is None


@pytest.mark.parametrize("github,msg", [
    ({"repos": {}}, "github.repos"),
    ({"repos": {"BEN": "FO-AI/benny"}}, "not in `prefixes`"),
    ({"repos": {"NIM": "nimbus"}}, "owner/name"),
    ({"repos": {"NIM": "FO-AI/nimbus"}, "renamed": {"benny": "FO-AI/quad"}}, "owner/name"),
    ({"repos": {"NIM": "FO-AI/nimbus"}, "limit_per_repo": 500}, "between 1 and 200"),
    ({"repos": {"NIM": "FO-AI/nimbus"}, "intake_types": []}, "at least one issue type"),
    ({"repos": {"NIM": "FO-AI/nimbus"}, "intake_labels": ["bug"]}, "replaced by intake_types"),
])
def test_bad_github_section_is_refused(tmp_path, github, msg):
    with pytest.raises(config.ConfigError, match=msg):
        _load(tmp_path, github=github)
