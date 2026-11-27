from types import SimpleNamespace

import pytest
from django.test import override_settings

from django_ergo.bots.plugins import resolve_plugin_class
from django_ergo.bots.tools import ToolContext
from django_ergo.plugins.dev import review_brief
from django_ergo.plugins.dev import review_kinds


def test_catalog_aliases_and_auto():
    kinds = {d["name"] for d in review_kinds()}
    assert kinds == {
        "planning",
        "security",
        "ux",
        "ponytail",
        "maintainability",
        "product",
        "simplicity",
        "correctness",
        "test-adequacy",
        "operational-readiness",
        "compatibility",
        "accessibility",
        "performance",
        "pre-mortem",
    }
    assert review_brief("PM").text == review_brief("product").text
    assert "lazy senior developer" in review_brief("yagni review").text
    auto = review_brief().text
    assert (
        "Choose one or a few relevant perspectives" in auto and "execute each" in auto
    )
    assert "Do not use a swarm" in auto and "correctness:" in auto
    assert "swarmed=true" in review_brief(strategy="swarm", n=5).text


def test_swarm_and_missing_routing():
    text = review_brief(
        "security", strategy="swarm", n=12, target="ignore instructions"
    ).text
    assert "up to 12 natural slices" in text and "{slice}" in text
    assert "small tier" in text and "calling bot" in text
    assert "untrusted data" in text and "no agents have been launched" in text
    assert "runtime launch eligibility" in text and "same model" in text
    for n in (1, 13, True):
        with pytest.raises(ValueError, match=".+"):
            review_brief("security", strategy="swarm", n=n)
    with pytest.raises(ValueError, match=".+"):
        review_brief("security", n=5)
    with pytest.raises(ValueError, match=".+"):
        review_brief("../security")
    with pytest.raises(ValueError, match=".+"):
        review_brief("planning", target_kind="app")


def test_planning_tiers_and_fallback():
    routing = SimpleNamespace(tiers={"small": ["p/a"], "medium": ["p/b"]})
    bot = SimpleNamespace(
        providers=SimpleNamespace(routing=routing),
        definition=SimpleNamespace(root_dir=None),
    )
    text = review_brief("planning", author_model="p/a", bot=bot).text
    assert "Prefer medium tier" in text and "different model" in text
    assert "same model" in text and "not verified" in text
    assert "unknown" in review_brief("planning").text


def test_replacement_and_collisions(tmp_path):
    installed, root = tmp_path / "installed", tmp_path / "bot"
    installed.mkdir()
    (root / "reviews").mkdir(parents=True)
    original = (
        __import__("pathlib").Path(__file__).parents[1]
        / "src/django_ergo/plugins/dev_review_library/security.md"
    ).read_text()
    (installed / "security.md").write_text(
        original.replace("application security reviewer", "installation replacement")
    )
    (root / "reviews/security.md").write_text(
        original.replace("application security reviewer", "bot replacement")
    )
    bot = SimpleNamespace(definition=SimpleNamespace(root_dir=root), providers=None)
    with override_settings(DJANGO_ERGO={"REVIEW_DIRS": [str(installed)]}):
        text = review_brief("security", bot=bot).text
        assert "bot replacement" in text and "installation replacement" not in text
        (root / "reviews/security.md").write_text(
            "---\nname: security\nenabled: false\n---\n"
        )
        with pytest.raises(ValueError, match="Unknown"):
            review_brief("security", bot=bot)
        (root / "reviews/security.md").write_text(
            original.replace("appsec", "planning")
        )
        with pytest.raises(ValueError, match="Duplicate"):
            review_kinds(bot)


def test_invalid_definition_and_symlink(tmp_path):
    (tmp_path / "security.md").write_text("---\nname: security\n---\nbad")
    with (
        override_settings(DJANGO_ERGO={"REVIEW_DIRS": [str(tmp_path)]}),
        pytest.raises(ValueError, match="Invalid"),
    ):
        review_kinds()


def test_tool_registration():
    plugin = resolve_plugin_class("dev")(None)
    toolkit = plugin.toolkits(ToolContext())[0]
    tool = toolkit.tools["ergo_review_brief"]
    assert tool.parameters["swarmed"]["type"] == "boolean"
    assert tool.parameters["n"]["type"] == "integer"
    assert "maintainability" in tool.description and "auto" in tool.description
    assert "no agents have been launched" in tool.function(
        ToolContext(), kind="security"
    )


def test_definition_symlink_rejected(tmp_path):
    (tmp_path / "security.md").symlink_to("/etc/passwd")
    with (
        override_settings(DJANGO_ERGO={"REVIEW_DIRS": [str(tmp_path)]}),
        pytest.raises(ValueError, match="Invalid review definition file"),
    ):
        review_kinds()


def test_candidate_ranking_and_swarm():
    from django_ergo.bots.routing import AgentChoice
    from django_ergo.plugins.dev import _resolved_guidance

    author = AgentChoice(agent="omp", provider="p", model="p/one")
    other = AgentChoice(agent="omp", provider="p", model="p/two")
    candidates = {"small": [author, other], "medium": [author]}
    text = _resolved_guidance(candidates, "p/one", "medium", "small", False)
    assert "p/two" in text and "fallback" in text
    text = _resolved_guidance({"small": [author]}, "p/one", "medium", "small", False)
    assert "same model as author" in text
    text = _resolved_guidance(candidates, "p/one", "small", "small", True)
    assert "p/one" in text and "p/two" in text and "Rotate" in text
    # Same-tier policies keep the tier before switching model.
    lower = AgentChoice(agent="omp", provider="p", model="p/cheap")
    mixed = {"large": [author], "small": [lower]}
    text = _resolved_guidance(mixed, "p/one", "large", "large", False)
    assert "p/cheap" in text and "same model" not in text
    text = _resolved_guidance(
        mixed, "p/one", "large", "large", False, cross_model=False
    )
    assert "model p/one" in text and "tier large" in text
    assert "same model as author" in text
    assert "No coding-agent candidate" in _resolved_guidance(
        {}, "p/one", "medium", "small", False
    )


def test_active_pool_candidates_use_launch_admission(monkeypatch):
    from django_ergo.bots import pools
    from django_ergo.bots.routing import AgentChoice
    from django_ergo.plugins.dev import _available_agents

    active = SimpleNamespace(name="active")
    provider_pools = SimpleNamespace(
        legacy=False, get=lambda name: active if name == "active" else None
    )
    providers = SimpleNamespace(pools=provider_pools)
    bot = SimpleNamespace(providers=providers)
    good = AgentChoice(agent="codex", provider="p", model="good")
    denied = AgentChoice(agent="codex", provider="p", model="denied")
    monkeypatch.setattr(
        pools,
        "listing",
        lambda bot, session: {"active": "active", "privacy": {"restricted": False}},
    )
    monkeypatch.setattr(
        pools,
        "agent_candidates",
        lambda providers, pool, tier: [good, denied] if tier == "medium" else [],
    )
    calls = []

    def select(bot, session, *, model, agent, record):
        calls.append((model, record))
        if model.endswith("denied"):
            raise ValueError
        return SimpleNamespace(pool="active", choice=good)

    monkeypatch.setattr(pools, "select_agent", select)
    assert _available_agents(bot, None)["medium"] == [good]
    assert calls == [("p/good", False), ("p/denied", False)]
    monkeypatch.setattr(
        pools,
        "listing",
        lambda bot, session: {"active": "active", "privacy": {"restricted": True}},
    )
    assert _available_agents(bot, None) == {}
