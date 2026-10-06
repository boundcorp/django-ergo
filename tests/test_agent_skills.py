"""The agent skills in Ergo's skill library, including page-design presentation recipes."""

import importlib.util
import textwrap
from types import SimpleNamespace

import pytest

from django_ergo.bots.runtime import Bot
from django_ergo.bots.skills import library_dir

AGENT_SKILLS = [
    "ergo-bot-development",
    "ergo-client",
    "ergo-developer",
    "ergo-hosting",
    "page-design",
]


def load_installer():
    spec = importlib.util.spec_from_file_location(
        "ergo_skill_install", library_dir() / "install.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_bots_can_include_the_agent_skills(tmp_path):
    folder = tmp_path / "helper"
    folder.mkdir()
    (folder / "agents.md").write_text("You help.")
    (folder / "bot.yaml").write_text(
        textwrap.dedent(
            f"""
            name: helper
            engine: {{type: claude, config: {{model: claude-test}}}}
            skills: {{include: [{", ".join(AGENT_SKILLS)}]}}
            """
        )
    )
    bot = Bot.load(folder)
    skills = {s.name: s for s in bot.skill_defs}
    for name in AGENT_SKILLS:
        assert skills[name].source == f"ergo:skill_library/{name}"
        assert skills[name].description
    module = bot.skill_tool_modules["ergo-client"]
    names = {t.name for t in module.tools}
    assert names == {
        "ergo_client_bots",
        "ergo_client_threads",
        "ergo_client_show",
        "ergo_client_new_thread",
        "ergo_client_send",
        "ergo_client_approve",
    }


def test_page_design_skill_loads_pages_and_attachment_tools(tmp_path):
    from django_ergo.bots.skillset import SkillSet
    from django_ergo.bots.tools import ToolContext

    folder = tmp_path / "planner"
    folder.mkdir()
    (folder / "agents.md").write_text("You plan clearly.")
    (folder / "bot.yaml").write_text(
        "name: planner\nskills: {include: [page-design]}\n"
    )
    bot = Bot.load(folder)
    skills = {skill.name: skill for skill in bot.skill_defs}

    page_design = skills["page-design"]
    assert page_design.source == "ergo:skill_library/page-design"
    assert page_design.requires == ["pages", "attachments"]
    assert bot.plugin("pages") is not None and bot.plugin("attachments") is not None

    class EmptyAttachments:
        def filter(self, **_):
            return self

        def order_by(self, *_):
            return self

        def __getitem__(self, _):
            return []

        def count(self):
            return 0

    ctx = ToolContext(bot=bot, session=SimpleNamespace(attachments=EmptyAttachments()))
    loaded = SkillSet(ctx, bot.skill_defs).load_result("page-design")
    assert "Pattern catalog" in loaded
    assert "ergo_page_write" in loaded
    assert "ergo_attachments_create" in loaded


def test_page_design_pattern_examples_render():
    from django_ergo.bots.pages import render_page

    bot = SimpleNamespace(
        name="patterns",
        definition=SimpleNamespace(root_dir=None, description=""),
        tables=[],
    )
    patterns = library_dir() / "page-design" / "patterns"
    expected_titles = {
        "roadmap.jhtml": "Summer campaign roadmap",
        "status-board.jhtml": "Moving-week status board",
        "dependency-table.jhtml": "Weekend trip dependencies",
        "decision-flow.jhtml": "Campaign approval flow",
        "timeline.jhtml": "Kyoto itinerary",
        "comparison-matrix.jhtml": "Channel comparison",
        "project-map.jhtml": "Hernandez house move",
    }

    for filename, title in expected_titles.items():
        html = render_page(bot, (patterns / filename).read_text())
        assert "<html" in html and title in html

    source = (patterns / "project-map.jhtml").read_text()
    html = render_page(bot, source)
    assert "You are here" in html
    assert "Waiting on the landlord&#39;s meter reading" in html
    assert "No reply by Friday 10:00" in html
    assert "Changed since last update" in html
    assert "--pd-accent:#8167dc" in html

    macro_only = source.split("{% set sample_counts", maxsplit=1)[0]
    minimal_html = render_page(
        bot,
        macro_only + '\n{{ project_map("Brief move", "", "", accent="#0088cc") }}',
    )
    assert "Brief move" in minimal_html
    assert "Milestones" not in minimal_html
    assert "--pd-accent:#0088cc" in minimal_html


def test_client_tools_need_a_server(tmp_path):
    spec = importlib.util.spec_from_file_location(
        "ergo_client_tools", library_dir() / "ergo-client" / "tools.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    ctx = SimpleNamespace(secret=lambda name, default=None: None)
    with pytest.raises(RuntimeError, match="ERGONAUT_URL and ERGONAUT_API_KEY"):
        module.run(ctx, "bots")


def test_installer_links_only_the_agent_skills(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    installer = load_installer()
    assert [s.name for s in installer.installable("claude")] == AGENT_SKILLS
    assert installer.main(["--bin", str(tmp_path / "bin")]) == 0
    for agent in ("claude", "codex"):
        skills = tmp_path / agent / "skills"
        assert sorted(p.name for p in skills.iterdir()) == AGENT_SKILLS
        assert (skills / "ergo-client").resolve() == library_dir() / "ergo-client"
        assert (
            (skills / "ergo-client" / "SKILL.md")
            .read_text()
            .startswith("---\nname: ergo-client\n")
        )
    assert (
        tmp_path / "bin" / "ergonaut-remote"
    ).resolve() == library_dir() / "ergo-client" / "scripts" / "ergonaut_remote.py"

    # Someone's own skill of the same name is left alone; reinstalling replaces ours.
    own = tmp_path / "codex" / "skills" / "ergo-hosting"
    own.unlink()
    own.mkdir()
    (own / "SKILL.md").write_text("mine")
    assert installer.main(["--target", "codex"]) == 0
    assert (own / "SKILL.md").read_text() == "mine"

    assert installer.main(["--uninstall", "--bin", str(tmp_path / "bin")]) == 0
    assert list((tmp_path / "claude" / "skills").iterdir()) == []
    assert [p.name for p in (tmp_path / "codex" / "skills").iterdir()] == [
        "ergo-hosting"
    ]
    assert not (tmp_path / "bin" / "ergonaut-remote").exists()


def test_installer_copies_without_bot_only_files(tmp_path):
    installer = load_installer()
    dest = tmp_path / "skills"
    assert installer.main(["--target", "codex", "--dest", str(dest), "--copy"]) == 0
    client = dest / "ergo-client"
    assert (client / "scripts" / "ergonaut_remote.py").is_file() and not (
        client / "tools.py"
    ).exists()
    assert (
        installer.main(["--target", "codex", "--dest", str(dest), "--uninstall"]) == 0
    )
    assert list(dest.iterdir()) == []
