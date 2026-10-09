"""Tests for the built-in bot plugins: ergo_kb, bot_management and telegram."""

from __future__ import annotations

import json
import subprocess
import textwrap

import pytest
from django.contrib.auth import get_user_model

from django_ergo.bots.runtime import Bot
from django_ergo.conversation.models import ConversationAttachment
from django_ergo.conversation.models import ConversationSession
from django_ergo.conversation.toolkit import Toolkit
from tests.test_bots import make_bot
from tests.test_bots import say
from tests.test_bots import write_bot
from tests.test_conversation_structured import claude_text
from tests.test_conversation_structured import claude_tool

User = get_user_model()


# ---------------------------------------------------------------------------
# ergo_kb
# ---------------------------------------------------------------------------


class FakeKB(Toolkit):
    searches: list = []

    def has_tool(self, tool_name):
        return tool_name == "kb_search"

    def execute_tool(self, tool_name, arguments):
        FakeKB.searches.append(arguments)
        if arguments["query"] == "boom":
            msg = "index offline"
            raise RuntimeError(msg)
        return f"Article 1A: Tacos (for {arguments['query']!r})"

    def get_tools_schema(self, adapter):
        return [{"name": "kb_search", "input_schema": {"type": "object"}}]

    def render_overview(self):
        return ""


def fake_kb(ctx):
    return FakeKB()


def kb_yaml(prefetch="new_session"):
    return f"""
        name: kitchen
        plugins:
          - name: ergo_kb
            toolkit: tests.test_bot_plugins:fake_kb
            prefetch: {prefetch}
            top_k: 3
    """


@pytest.mark.django_db(transaction=True)
async def test_kb_prefetch_on_new_session_only(tmp_path):
    FakeKB.searches = []
    user = await User.objects.acreate(username="kb-new")
    bot, engine = make_bot(tmp_path, say("Tacos."), say("Ok."), yaml_text=kb_yaml())
    thread = await bot.create_session(user)

    await bot.ask(thread, "dinner ideas")
    first = engine._client.calls[0]
    assert "## Knowledge base results for this message" in _turn_context(first)
    assert "Article 1A: Tacos (for 'dinner ideas')" in _turn_context(first)
    # Prefetch works whether or not the kb skill is loaded; its tools wait.
    assert "kb_search" not in {t["name"] for t in first["tools"]}
    assert FakeKB.searches == [{"query": "dinner ideas", "top_k": 3}]

    await bot.ask(thread, "and lunch?")
    assert "Knowledge base results" not in _turn_context(engine._client.calls[1])
    assert len(FakeKB.searches) == 1


@pytest.mark.django_db(transaction=True)
async def test_kb_prefetch_every_turn_survives_errors(tmp_path):
    FakeKB.searches = []
    user = await User.objects.acreate(username="kb-every")
    bot, engine = make_bot(
        tmp_path,
        say("a"),
        say("b"),
        yaml_text=kb_yaml("every_turn"),
    )
    root = await bot.root_session(user)
    await bot.ask(root, "first")
    await bot.ask(root, "boom")
    assert "for 'first'" in _turn_context(engine._client.calls[0])
    assert "prefetch failed: index offline" in _turn_context(engine._client.calls[1])


@pytest.mark.django_db
def test_kb_plugin_config(tmp_path):
    from django_ergo.kb_toolkit import KBToolkit
    from django_ergo.models import Knowledgebase

    Knowledgebase.objects.create(name="Kitchen", description="Recipes")
    yaml_text = """
        name: kb
        plugins: [{name: ergo_kb, knowledgebases: [Kitchen], prefetch: off}]
    """
    bot = Bot.load(write_bot(tmp_path, yaml_text, name="kb"))
    plugin = bot.plugin("ergo_kb")
    toolkit = plugin.make_toolkit(None)
    assert isinstance(toolkit, KBToolkit)
    assert list(toolkit._name_to_id) == ["Kitchen"]
    assert plugin.context_sources(None, "hello") == []

    bad = """
        name: kb2
        plugins: [{name: ergo_kb, knowledgebases: [Nope]}]
    """
    bot = Bot.load(write_bot(tmp_path, bad, name="kb2"))
    with pytest.raises(ValueError, match="Unknown knowledge bases: Nope"):
        bot.plugin("ergo_kb").make_toolkit(None)
    with pytest.raises(ValueError, match="needs path, knowledgebases or toolkit"):
        Bot.load(write_bot(tmp_path, "name: kb3\nplugins: [ergo_kb]\n", name="kb3"))
    with pytest.raises(ValueError, match="prefetch must be one of"):
        Bot.load(
            write_bot(
                tmp_path,
                "name: kb4\nplugins: [{name: ergo_kb, toolkit: x:y, prefetch: x}]\n",
                name="kb4",
            )
        )


# ---------------------------------------------------------------------------
# bot_management
# ---------------------------------------------------------------------------


def git(cwd, *args):
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout


@pytest.fixture
def bot_repo(tmp_path, monkeypatch):
    """A bare remote and a clone whose bots/manager folder is a bot."""
    for key, value in {
        "GIT_AUTHOR_NAME": "Bot",
        "GIT_AUTHOR_EMAIL": "bot@example.com",
        "GIT_COMMITTER_NAME": "Bot",
        "GIT_COMMITTER_EMAIL": "bot@example.com",
    }.items():
        monkeypatch.setenv(key, value)
    remote = tmp_path / "remote.git"
    git(tmp_path, "init", "--bare", "-b", "main", str(remote))
    work = tmp_path / "work"
    git(tmp_path, "clone", str(remote), str(work))
    git(work, "checkout", "-b", "main")
    (work / "README.md").write_text("bots\n")
    git(work, "add", "-A")
    git(work, "commit", "-m", "init")
    git(work, "push", "-u", "origin", "main")
    return remote, work


def management_bot(work, mode, *responses, root_only=False):
    yaml_text = f"""
        name: manager
        chats: {{main: {{skills: [config_repo]}}}}
        plugins: [{{name: bot_management, mode: {mode}, root_only: {str(root_only).lower()}}}]
    """
    folder = work / "bots"
    folder.mkdir(exist_ok=True)
    bot, engine = make_bot(folder, *responses, yaml_text=yaml_text, name="manager")
    return bot, engine, bot.plugin("bot_management")


@pytest.mark.django_db(transaction=True)
async def test_bot_edits_and_merges_its_own_config(bot_repo):
    remote, work = bot_repo
    bot, engine, plugin = management_bot(
        work,
        "merge_main",
        claude_tool(
            "ergo_config_repo_write",
            {"path": "bots/manager/agents.md", "content": "Be brief."},
            tool_id="w1",
        ),
        claude_tool(
            "ergo_config_repo_publish",
            {"message": "Shorter instructions"},
            tool_id="p1",
        ),
        say("Published."),
    )
    git(work, "add", "-A")
    git(work, "commit", "-m", "add bot")
    git(work, "push")
    user = await User.objects.acreate(username="manager")
    root = await bot.root_session(user)

    paused = await bot.ask(root, "Make your instructions shorter")
    # merge_main edits the running bots, so the write itself needs approval.
    assert [a.tool_name for a in paused.approvals] == ["ergo_config_repo_write"]
    assert (work / "bots/manager/agents.md").read_text() != "Be brief."
    paused = await bot.resume(root, {"w1": True})
    assert [a.tool_name for a in paused.approvals] == ["ergo_config_repo_publish"]
    assert (work / "bots/manager/agents.md").read_text() == "Be brief."
    assert "Shorter" not in git(remote, "log", "--oneline", "main")

    done = await bot.resume(root, {"p1": True})
    assert done.text == "Published."
    assert "Shorter instructions" in git(remote, "log", "--oneline", "main")
    assert "Pushed" in engine._client.calls[-1]["messages"][-1]["content"][0]["content"]
    assert "(clean)" in plugin.status()
    assert plugin.publish("again") == "Nothing to publish: there are no changes."


@pytest.mark.django_db
def test_bot_proposes_pr_and_returns_to_main(bot_repo, monkeypatch):
    remote, work = bot_repo
    bot, _, plugin = management_bot(work, "propose_pr")
    git(work, "add", "-A")
    git(work, "commit", "-m", "add bot")
    git(work, "push")
    calls = []
    real_run = plugin.run

    def run(args, cwd=None):
        if args[0] == "gh":
            calls.append(args)
            return "https://github.com/acme/bots/pull/7\n"
        return real_run(args, cwd)

    monkeypatch.setattr(plugin, "run", run)

    plugin.write("bots/manager/tools/new.py", "x = 1\n")
    assert "bots/manager/tools/new.py" in plugin.diff()
    assert "bots/manager/tools/new.py" in plugin.list_files("bots/manager")
    # The draft lives beside the checkout: the running bots don't see it.
    assert not (work / "bots/manager/tools/new.py").exists()
    assert git(work, "status", "--porcelain").strip() == ""
    result = plugin.publish("Add a tool", title="New tool")

    assert result.startswith(
        "Opened https://github.com/acme/bots/pull/7 from bot/manager/"
    )
    branch = result.split(" from ")[1].split(";")[0]
    assert branch.endswith("-new-tool")
    assert calls[0][:3] == ["gh", "pr", "create"]
    assert calls[0][calls[0].index("--head") + 1] == branch
    assert "Add a tool" in git(remote, "log", "--oneline", branch)
    assert git(work, "rev-parse", "--abbrev-ref", "HEAD").strip() == "main"
    assert not (work / "bots/manager/tools/new.py").exists()
    assert result.endswith("it goes live once merged.")
    assert not plugin.draft_dir.exists()
    # The next draft starts again from main.
    assert "(clean)" in plugin.status()

    with pytest.raises(ValueError, match="outside the repository"):
        plugin.read("../remote.git/config")
    with pytest.raises(ValueError, match="outside the repository"):
        plugin.write(".git/hooks/pre-commit", "evil")


def test_bot_management_runs_configured_commands_in_the_draft(bot_repo, monkeypatch):
    _, work = bot_repo
    yaml_text = """
        name: manager
        chats: {main: {skills: [config_repo]}}
        plugins:
          - name: bot_management
            mode: propose_pr
            run:
              approve: false
              timeout: 5
              commands:
                check: {argv: [python3, check.py], cwd: tools}
                py: {argv: [python3], cwd: tools, args: true}
    """
    folder = work / "bots"
    folder.mkdir(exist_ok=True)
    bot, _ = make_bot(folder, yaml_text=yaml_text, name="manager")
    plugin = bot.plugin("bot_management")
    git(work, "add", "-A")
    git(work, "commit", "-m", "add bot")
    git(work, "push")
    monkeypatch.setenv("SECRET_TOKEN", "hunter2")
    plugin.write(
        "tools/check.py",
        "import os, sys; print('ok', os.environ.get('SECRET_TOKEN')); sys.exit(3)\n",
    )
    plugin.write("tools/hello.py", "import sys; print('hi', sys.argv[1])\n")

    # It runs the draft (not the checkout), without the server's secrets.
    assert plugin.run_command("check") == "check: exit 3\nok None"
    assert plugin.run_command("py", ["hello.py", "there"]) == "py: exit 0\nhi there"
    with pytest.raises(ValueError, match="takes no arguments"):
        plugin.run_command("check", ["x"])
    with pytest.raises(ValueError, match="configured: check, py"):
        plugin.run_command("rm")
    tools = {t.name: t for t in plugin._tools()}
    assert not tools["ergo_config_repo_run"].requires_approval
    assert "py (takes arguments)" in tools["ergo_config_repo_run"].description

    # Without run.commands there is no tool.
    plugin.run_commands = {}
    assert "ergo_config_repo_run" not in {t.name for t in plugin._tools()}


def test_bot_management_reads_edits_and_greps_files(bot_repo, monkeypatch):
    _, work = bot_repo
    _, _, plugin = management_bot(work, "propose_pr")
    git(work, "add", "-A")
    git(work, "commit", "-m", "add bot")
    git(work, "push")
    plugin.write("notes.txt", "one\ntwo\none\n")

    assert plugin.read("notes.txt", start_line=2, end_line=2) == (
        "notes.txt lines 2-2 of 3\n2: two"
    )
    assert plugin.grep("two", "notes.txt") == "notes.txt:2:two"
    assert plugin.edit("README.md", "bots", "configured bots") == (
        "Edited README.md (1 replacement)"
    )
    assert (work / "README.md").read_text() == "bots\n"
    with pytest.raises(ValueError, match="wasn't found"):
        plugin.edit("notes.txt", "three", "THREE")
    with pytest.raises(ValueError, match="matches 2 times"):
        plugin.edit("notes.txt", "one", "ONE")
    assert plugin.edit("notes.txt", "two", "TWO") == "Edited notes.txt (1 replacement)"
    assert plugin.edit("notes.txt", "one", "ONE", replace_all=True) == (
        "Edited notes.txt (2 replacements)"
    )
    assert plugin.read("notes.txt") == "ONE\nTWO\nONE\n"
    with pytest.raises(ValueError, match="outside"):
        plugin.edit("../remote.git/config", "x", "y")

    monkeypatch.setattr("django_ergo.plugins.bot_management.MAX_READ_CHARS", 8)
    truncated = plugin.read("notes.txt")
    assert "3 total lines" in truncated
    assert "start_line=3" in truncated


def test_bot_management_pull_ignores_multiple_tracking_branches(bot_repo, tmp_path):
    remote, work = bot_repo
    _, _, plugin = management_bot(work, "merge_main")
    git(work, "add", "-A")
    git(work, "commit", "-m", "add bot")
    git(work, "push")
    git(work, "branch", "other")
    git(work, "push", "origin", "other")
    git(work, "config", "--add", "branch.main.merge", "refs/heads/other")

    old_pull = subprocess.run(
        ["git", "pull", "--rebase"],
        cwd=work,
        capture_output=True,
        text=True,
        check=False,
    )
    assert old_pull.returncode
    assert "Cannot rebase onto multiple branches" in old_pull.stderr

    _merge_on_remote(tmp_path, remote, "pulled.txt", "from main", "remote update")
    assert "Fast-forward" in plugin.pull()
    assert (work / "pulled.txt").read_text() == "from main"


def test_bot_management_pull_names_diverged_main(bot_repo, tmp_path):
    remote, work = bot_repo
    _, _, plugin = management_bot(work, "merge_main")
    git(work, "add", "-A")
    git(work, "commit", "-m", "add bot")
    git(work, "push")
    _merge_on_remote(tmp_path, remote, "remote.txt", "remote", "remote update")
    (work / "local.txt").write_text("local")
    git(work, "add", "local.txt")
    git(work, "commit", "-m", "local update")

    with pytest.raises(ValueError, match="has diverged from origin/main"):
        plugin.pull()


@pytest.mark.django_db
def test_bot_discards_a_draft(bot_repo):
    _, work = bot_repo
    _, _, plugin = management_bot(work, "propose_pr")
    git(work, "add", "-A")
    git(work, "commit", "-m", "add bot")
    git(work, "push")
    plugin.write("bots/manager/agents.md", "Be loud.")
    assert "Be loud." in plugin.diff()
    assert plugin.discard() == "Discarded the unpublished changes."
    assert plugin.diff() == "(no changes)"
    assert "bot/manager/draft" in git(work, "branch")


def test_bot_moves_a_file_by_writing_then_deleting(bot_repo):
    _, work = bot_repo
    _, _, plugin = management_bot(work, "propose_pr")
    git(work, "add", "-A")
    git(work, "commit", "-m", "add bot")
    git(work, "push")
    assert "ergo_config_repo_delete" in [tool.name for tool in plugin._tools()]
    old = plugin.read("bots/manager/agents.md")
    plugin.write("bots/renamed/agents.md", old)
    assert plugin.delete("bots/manager/agents.md") == "Deleted bots/manager/agents.md"
    diff = plugin.diff()
    assert (
        "rename from bots/manager/agents.md" in diff
        and "rename to bots/renamed/agents.md" in diff
    )
    with pytest.raises(ValueError, match="doesn't exist"):
        plugin.delete("bots/manager/agents.md")
    with pytest.raises(ValueError, match="is a folder"):
        plugin.delete("bots/manager")
    with pytest.raises(ValueError, match="outside"):
        plugin.delete("../x")
    assert (
        work / "bots" / "manager" / "agents.md"
    ).exists()  # the live checkout is untouched


@pytest.mark.django_db(transaction=True)
async def test_bot_management_tools_reach_threads_unless_root_only(bot_repo):
    _, work = bot_repo
    load = claude_tool("ergo_skill_load", {"name": "config_repo"})
    bot, engine, plugin = management_bot(
        work, "merge_main", load, say("a"), load, say("b")
    )
    user = await User.objects.acreate(username="threads")
    root = await bot.root_session(user)

    async def thread_tools():
        await bot.ask(await bot.create_session(user, parent=root), "hi")
        return {t["name"] for t in engine._client.calls[-1]["tools"]}

    # By default a task thread can change its own folder.
    assert "ergo_config_repo_publish" in await thread_tools()
    # root_only: true keeps the tools to top-level chats.
    plugin.root_only = True
    assert "ergo_config_repo_publish" not in await thread_tools()


# ---------------------------------------------------------------------------
# telegram
# ---------------------------------------------------------------------------


class FakeTelegram:
    def __init__(self, updates=()):
        self.updates = list(updates)
        self.calls = []

    async def call(self, method, **params):
        self.calls.append((method, params))
        if method == "getUpdates":
            updates, self.updates = self.updates, []
            return updates
        return True

    async def download(self, file_id):
        return b"\x89PNG fake " + file_id.encode()

    def sent(self):
        return [p for m, p in self.calls if m == "sendMessage"]


TELEGRAM_TOOLS = textwrap.dedent(
    """
    from django_ergo.bots import bot_tool

    @bot_tool(requires_approval=True)
    def order(item: str) -> str:
        return f"ordered {item}"
    """
)


def telegram_bot(tmp_path, *responses):
    yaml_text = """
        name: kitchen
        tools: [tools/pantry.py]
        plugins:
          - name: telegram
            token_env: TEST_TELEGRAM_TOKEN
            users: {111: cook}
    """
    bot, engine = make_bot(
        tmp_path, *responses, yaml_text=yaml_text, tools=TELEGRAM_TOOLS
    )
    plugin = bot.plugin("telegram")
    plugin.api = FakeTelegram()
    return bot, engine, plugin


def update(update_id, chat_id=111, **message):
    return {"update_id": update_id, "message": {"chat": {"id": chat_id}, **message}}


@pytest.mark.django_db(transaction=True)
async def test_telegram_routes_chats_to_root_sessions(tmp_path):
    user = await User.objects.acreate(username="cook")
    bot, engine, plugin = telegram_bot(tmp_path, say("Hello cook!"))
    plugin.api.updates = [
        update(5, text="hi"),
        update(6, chat_id=999, text="who am I?"),  # unknown chat: ignored
    ]

    assert await plugin.poll_once() == 2

    assert plugin._offset == 7
    assert plugin.api.sent() == [{"chat_id": 111, "text": "Hello cook!"}]
    root = await bot.root_session(user)
    assert len(engine._client.calls) == 1
    assert await bot.sessions().acount() == 1
    assert await plugin.notify(user, "Dinner is ready")
    assert plugin.api.sent()[-1] == {"chat_id": "111", "text": "Dinner is ready"}
    assert root.bot_name == "kitchen"


@pytest.mark.django_db(transaction=True)
async def test_telegram_photo_becomes_attachment(tmp_path):
    await User.objects.acreate(username="cook")
    bot, engine, plugin = telegram_bot(tmp_path, say("Nice lasagna."))

    await plugin.handle_update(
        update(1, caption="what is this?", photo=[{"file_id": "s"}, {"file_id": "big"}])
    )

    content = engine._client.calls[0]["messages"][0]["content"]
    assert content[0]["type"] == "text"
    assert content[0]["text"].startswith("<turn-context>")
    assert content[1]["type"] == "image"
    assert content[-1] == {"type": "text", "text": "what is this?"}
    attachment = await ConversationAttachment.objects.aget()
    assert attachment.metadata == {"telegram_file_id": "big"}
    assert attachment.kind == "image"


@pytest.mark.django_db(transaction=True)
async def test_telegram_approval_buttons_resume_the_turn(tmp_path):
    await User.objects.acreate(username="cook")
    bot, engine, plugin = telegram_bot(
        tmp_path,
        claude_tool("order", {"item": "flour"}, tool_id="o1", text="Ordering."),
        say("Flour is on the way."),
    )

    await plugin.handle_update(update(1, text="We need flour"))
    prompt = plugin.api.sent()[-1]
    assert prompt["text"] == "Approve order?"
    session = await ConversationSession.objects.aget()
    call = await session.structured_calls.aget()
    buttons = prompt["reply_markup"]["inline_keyboard"][0]
    assert buttons[0]["callback_data"].startswith(f"ok:{call.id}:")
    assert len(buttons[0]["callback_data"]) <= 64

    # Someone else in the chat can't answer for the user.
    stranger = {
        "id": "cb0",
        "data": buttons[0]["callback_data"],
        "from": {"id": 999},
        "message": {"chat": {"id": 111}},
    }
    await plugin.handle_update({"update_id": 2, "callback_query": stranger})
    assert plugin.api.sent()[-1]["text"] == "Approve order?"

    callback = {**stranger, "id": "cb1", "from": {"id": 111}}
    await plugin.handle_update({"update_id": 3, "callback_query": callback})

    assert ("answerCallbackQuery", {"callback_query_id": "cb1"}) in plugin.api.calls
    assert plugin.api.sent()[-1]["text"] == "Flour is on the way."
    result = engine._client.calls[-1]["messages"][-1]["content"][0]
    assert result["content"] == "ordered flour"

    # Pressing again finds nothing left to approve.
    await plugin.handle_update({"update_id": 4, "callback_query": callback})
    assert plugin.api.sent()[-1]["text"] == "That request was already handled."
    assert json.dumps(plugin.api.calls)  # calls are plain data


def test_telegram_needs_token(tmp_path, monkeypatch):
    monkeypatch.delenv("TEST_TELEGRAM_TOKEN", raising=False)
    yaml_text = (
        "name: tg\nplugins: [{name: telegram, token_env: TEST_TELEGRAM_TOKEN}]\n"
    )
    bot = Bot.load(write_bot(tmp_path, yaml_text, name="tg"))
    with pytest.raises(RuntimeError, match="TEST_TELEGRAM_TOKEN"):
        _ = bot.plugin("telegram").api
    monkeypatch.setenv("TEST_TELEGRAM_TOKEN", "123:abc")
    assert bot.plugin("telegram").api.token == "123:abc"


# ---------------------------------------------------------------------------
# ergo_kb with a Markdown folder
# ---------------------------------------------------------------------------


def folder_kb_bot(tmp_path, *responses, prefetch="every_turn"):
    yaml_text = f"""
        name: kitchen
        chats: {{main: {{skills: [kb]}}}}
        plugins:
          - name: ergo_kb
            path: ../kb
            prefetch: {prefetch}
    """
    kb = tmp_path / "kb"
    (kb / "preferences").mkdir(parents=True)
    (kb / "preferences" / "diet.md").write_text(
        "# Diet\n\nLee is vegetarian on weekdays. No cilantro, ever.\n"
    )
    (kb / "household.md").write_text("# Household\n\nTwo adults and a dog.\n")
    return make_bot(tmp_path, *responses, yaml_text=yaml_text)


@pytest.mark.django_db(transaction=True)
async def test_folder_kb_tools_and_prefetch(tmp_path):
    user = await User.objects.acreate(username="kb-folder")
    bot, engine = folder_kb_bot(
        tmp_path,
        claude_tool("ergo_kb_read", {"path": "preferences/diet.md"}),
        say("No cilantro, noted."),
        say("Hello!"),
    )
    root = await bot.root_session(user)

    await bot.ask(root, "Does anyone avoid cilantro?")
    first = engine._client.calls[0]
    assert {"ergo_kb_search", "ergo_kb_read", "ergo_kb_list"} <= {
        t["name"] for t in first["tools"]
    }
    assert "## Knowledge base results for this message" in _turn_context(first)
    assert "### Diet (preferences/diet.md)" in _turn_context(first)
    read = engine._client.calls[1]["messages"][-1]["content"][0]["content"]
    assert read.startswith("# preferences/diet.md")
    assert "No cilantro, ever." in read

    # Nothing relevant: no KB section at all.
    await bot.ask(root, "hello")
    assert "Knowledge base results" not in _turn_context(engine._client.calls[2])


def test_folder_kb_reads_only_markdown_inside(tmp_path):
    from django_ergo.bots.folder_kb import FolderKB

    kb_dir = tmp_path / "kb"
    kb_dir.mkdir()
    (kb_dir / "a.md").write_text("# Alpha\n\nalpha alpha beta")
    (kb_dir / "b.md").write_text("beta only")
    (tmp_path / "secret.md").write_text("secret")
    kb = FolderKB(kb_dir)

    assert [a.path for a, _ in kb.search("beta alpha")] == ["a.md", "b.md"]
    assert kb.read("a.md").title == "Alpha"
    assert kb.read("b.md").title == "b.md"
    with pytest.raises(ValueError, match="not an article"):
        kb.read("../secret.md")
    with pytest.raises(ValueError, match="No article"):
        kb.read("missing.md")
    assert kb.search("zz") == []
    assert FolderKB(tmp_path / "nope").articles() == []
    toolkit = kb.toolkit()
    assert toolkit.execute_tool("ergo_kb_list", {}) == "a.md: Alpha\nb.md: b.md"


@pytest.mark.django_db(transaction=True)
async def test_telegram_shows_suggestions_as_keyboard(tmp_path):
    await User.objects.acreate(username="cook")
    bot, engine, plugin = telegram_bot(
        tmp_path, say("Tacos or soup?", suggestions=["Tacos", "Soup"], kind="question")
    )

    await plugin.handle_update(update(1, text="Dinner?"))

    sent = plugin.api.sent()[-1]
    assert sent["text"] == "Tacos or soup?"
    assert sent["reply_markup"]["keyboard"] == [[{"text": "Tacos"}], [{"text": "Soup"}]]
    assert sent["reply_markup"]["one_time_keyboard"] is True


@pytest.mark.django_db(transaction=True)
async def test_telegram_group_chat_speaks_as_each_sender(tmp_path):
    cook = await User.objects.acreate(username="cook")
    await User.objects.acreate(username="sous")
    bot, engine, plugin = telegram_bot(tmp_path, say("Hi cook."), say("Hi sous."))
    plugin.users["222"] = "sous"
    group = -500

    await plugin.handle_update(
        update(
            1,
            chat_id=group,
            text="hi",
            **{"from": {"id": 111, "first_name": "Telegram Cook"}},
        )
    )
    await plugin.handle_update(
        update(2, chat_id=group, text="yo", **{"from": {"id": 222}})
    )
    await plugin.handle_update(
        update(3, chat_id=group, text="??", **{"from": {"id": 333}})
    )

    assert [m["chat_id"] for m in plugin.api.sent()] == [group, group]
    assert len(engine._client.calls) == 2
    owners = [s.user_id async for s in bot.sessions().order_by("created_at")]
    assert owners[0] == cook.id
    assert len(owners) == 2
    root = await bot.root_session(cook)
    incoming = await root.messages.filter(role="user").afirst()
    assert incoming.author == {
        "kind": "telegram_user",
        "ref": "111",
        "display_name": "Telegram Cook",
    }
    assert await incoming.content_blocks.filter(text="hi").aexists()


@pytest.mark.django_db(transaction=True)
async def test_telegram_album_is_one_turn(tmp_path):
    await User.objects.acreate(username="cook")
    bot, engine, plugin = telegram_bot(tmp_path, say("Two receipts."))
    plugin.album_wait = 60

    for n, file_id in enumerate(["r1", "r2"]):
        await plugin.handle_update(
            update(
                n,
                media_group_id="album-1",
                photo=[{"file_id": file_id}],
                **({"caption": "add these to the pantry"} if n == 0 else {}),
            )
        )
    assert engine._client.calls == []

    await plugin.flush_albums()

    assert len(engine._client.calls) == 1
    content = engine._client.calls[0]["messages"][0]["content"]
    assert [part["type"] for part in content] == ["text", "image", "image", "text"]
    assert content[-1]["text"] == "add these to the pantry"
    assert plugin.api.sent()[-1]["text"] == "Two receipts."


@pytest.mark.django_db(transaction=True)
async def test_telegram_webhook_mode(tmp_path, settings, monkeypatch):
    from django.test import AsyncClient

    from django_ergo.bots import webhooks
    from django_ergo.bots.registry import BotRegistry
    from django_ergo.settings import api_settings

    monkeypatch.setattr(
        api_settings, "BOT_WEBHOOK_BASE_URL", "https://bots.test/hooks/", raising=False
    )
    await User.objects.acreate(username="cook")
    bot, engine, plugin = telegram_bot(tmp_path, say("Hello from a webhook."))
    plugin.api.token = "123:abc"
    registry = BotRegistry()
    registry.add(bot)
    webhooks.set_registry(lambda: registry)  # loaded on first request
    try:
        assert plugin.uses_webhook
        await bot.serve()  # registers the webhook and returns
        method, params = plugin.api.calls[-1]
        assert method == "setWebhook"
        assert params["url"] == "https://bots.test/hooks/kitchen/telegram/update/"
        assert params["secret_token"] == plugin.webhook_secret

        client = AsyncClient()
        body = json.dumps(update(9, text="hi"))
        url = "/hooks/kitchen/telegram/update/"
        denied = await client.post(url, body, content_type="application/json")
        assert denied.status_code == 403
        missing = await client.post(
            "/hooks/kitchen/nope/update/", body, content_type="application/json"
        )
        assert missing.status_code == 404

        ok = await client.post(
            url,
            body,
            content_type="application/json",
            headers={"X-Telegram-Bot-Api-Secret-Token": plugin.webhook_secret},
        )
        assert ok.status_code == 200
        await plugin.drain()
        assert plugin.api.sent()[-1] == {
            "chat_id": 111,
            "text": "Hello from a webhook.",
        }
    finally:
        webhooks.set_registry(None)


def test_telegram_polling_without_public_url(tmp_path):
    bot, _, plugin = telegram_bot(tmp_path)
    assert plugin.uses_webhook is False
    plugin.mode = "webhook"
    with pytest.raises(RuntimeError, match="BOT_WEBHOOK_BASE_URL"):
        _ = plugin.uses_webhook


# ---------------------------------------------------------------------------
# orca
# ---------------------------------------------------------------------------


def orca_bot(tmp_path, *responses, config="environment: devbox, executable: orca-test"):
    yaml_text = f"""
        name: cto
        chats: {{main: {{skills: [orca]}}}}
        plugins: [{{name: orca, {config}}}]
    """
    bot, engine = make_bot(tmp_path, *responses, yaml_text=yaml_text, name="cto")
    return bot, engine, bot.plugin("orca")


@pytest.fixture
def orca_calls(monkeypatch):
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout='{"ok": true}', stderr="")

    monkeypatch.setattr("django_ergo.plugins.orca.subprocess.run", fake_run)
    return calls


@pytest.mark.django_db
def test_orca_read_runs_inventory_pinned_to_the_environment(tmp_path, orca_calls):
    _, _, plugin = orca_bot(tmp_path)
    assert plugin.read(["worktree", "ps"]) == '{"ok":true}'
    assert orca_calls[-1] == [
        "orca-test",
        "worktree",
        "ps",
        "--environment",
        "devbox",
        "--json",
    ]
    plugin.read(["skills", "get", "orchestration"])
    assert "--json" not in orca_calls[-1]
    plugin.read(["orchestration", "worker-start", "--help"])
    assert len(orca_calls) == 3


@pytest.mark.django_db
def test_orca_read_refuses_changes_and_other_environments(tmp_path, orca_calls):
    _, _, plugin = orca_bot(tmp_path)
    assert "use orca_run" in plugin.read(
        ["orchestration", "worker-start", "--task", "t1"]
    )
    assert "use orca_run" in plugin.read(["terminal", "send", "--text", "hi"])
    with pytest.raises(ValueError, match="only manages the 'devbox'"):
        plugin.run(["status", "--environment=prod"])
    assert orca_calls == []


@pytest.mark.django_db(transaction=True)
async def test_orca_run_waits_for_approval(tmp_path, orca_calls):
    bot, engine, _ = orca_bot(
        tmp_path,
        claude_tool(
            "orca_read", {"args": ["orchestration", "worker-list"]}, tool_id="r1"
        ),
        claude_tool(
            "orca_run",
            {"args": ["orchestration", "worker-stop", "--dispatch", "d1"]},
            tool_id="w1",
        ),
        say("Stopped."),
    )
    user = await User.objects.acreate(username="cto-user")
    root = await bot.root_session(user)

    paused = await bot.ask(root, "Stop worker d1")
    assert [a.tool_name for a in paused.approvals] == ["orca_run"]
    assert [c[1:3] for c in orca_calls] == [["orchestration", "worker-list"]]
    assert "Orca" in _turn_context(engine._client.calls[0])

    done = await bot.resume(root, {"w1": True})
    assert done.text == "Stopped."
    assert orca_calls[-1][1:3] == ["orchestration", "worker-stop"]


@pytest.mark.django_db
def test_orca_compacts_json_and_keeps_requested_fields(tmp_path, monkeypatch):
    listing = {
        "id": "req-1",
        "ok": True,
        "result": {
            "worktrees": [
                {"id": "r::/a", "path": "/a", "git": {"head": "x" * 50}},
                {"id": "r::/b", "path": "/b", "git": {"head": "y" * 50}},
            ]
        },
    }

    def fake_run(argv, **kwargs):
        stdout = json.dumps(listing, indent=2)
        return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr="")

    monkeypatch.setattr("django_ergo.plugins.orca.subprocess.run", fake_run)
    _, _, plugin = orca_bot(tmp_path)
    full = plugin.read(["worktree", "list"])
    assert full == json.dumps(listing["result"], separators=(",", ":"))
    short = plugin.read(["worktree", "list"], fields=["id"])
    assert json.loads(short) == {"worktrees": [{"id": "r::/a"}, {"id": "r::/b"}]}


@pytest.mark.django_db
def test_orca_reports_a_missing_cli(tmp_path):
    _, _, plugin = orca_bot(tmp_path, config="executable: no-such-orca-cli")
    assert "is not installed" in plugin.read(["status"])


# ---------------------------------------------------------------------------
# kubectl
# ---------------------------------------------------------------------------


def kubectl_bot(
    tmp_path,
    *responses,
    config="""\
clusters:
  configured:
    kubeconfig: /mounted/kubeconfig
    namespace: default
executable: kubectl-test""",
):
    yaml_text = f"""
        name: kube
        chats: {{main: {{skills: [kubectl]}}}}
        plugins:
          - name: kubectl
{textwrap.indent(config, "            ")}
    """
    bot, engine = make_bot(tmp_path, *responses, yaml_text=yaml_text, name="kube")
    return bot, engine, bot.plugin("kubectl")


@pytest.fixture
def kubectl_calls(monkeypatch):
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="pod/example", stderr="")

    monkeypatch.setattr("django_ergo.plugins.kubectl.subprocess.run", fake_run)
    return calls


@pytest.mark.django_db
def test_kubectl_read_is_pinned_to_a_configured_cluster(tmp_path, kubectl_calls):
    _, _, plugin = kubectl_bot(tmp_path)

    assert plugin.read("configured", ["get", "pods"]) == "Exit 0\npod/example"
    assert kubectl_calls[-1] == [
        "kubectl-test",
        "get",
        "pods",
        "--kubeconfig",
        "/mounted/kubeconfig",
        "--namespace",
        "default",
    ]
    plugin.read("configured", ["get", "pods", "-n", "other"])
    assert kubectl_calls[-1][-2:] == ["--kubeconfig", "/mounted/kubeconfig"]
    with pytest.raises(ValueError, match="not configured"):
        plugin.read("other", ["get", "pods"])
    with pytest.raises(ValueError, match="not configured"):
        plugin.run("other", ["delete", "pod", "example"])


@pytest.mark.django_db
def test_kubectl_read_refuses_aliases_sensitive_flags_and_secret_formats(
    tmp_path, kubectl_calls
):
    _, _, plugin = kubectl_bot(tmp_path)

    assert "only permits" in plugin.read("configured", ["g", "pods"])
    assert "only permits" in plugin.read("configured", ["get pods"])
    assert (
        "pins kubeconfig"
        in pytest.raises(
            ValueError,
            plugin.read,
            "configured",
            ["get", "pods", "--context=other"],
        ).value.args[0]
    )
    assert "Secret reads" in plugin.read("configured", ["get", "secrets.v1", "-ojson"])
    assert "does not permit output formats" in plugin.read(
        "configured",
        ["get", "pods,secrets", "-ojsonpath={.items[*].data.token}"],
    )
    assert kubectl_calls == []


def test_kubectl_redacts_nested_secret_data_from_json_and_yaml():
    from django_ergo.plugins.kubectl import redact_secret_data

    json_output = json.dumps(
        {
            "items": [
                {"data": {"token": "secret-value"}},
                {"nested": {"stringData": {"password": "another-secret"}}},
            ]
        }
    )
    redacted = redact_secret_data(json_output)
    assert "secret-value" not in redacted
    assert "another-secret" not in redacted
    assert json.loads(redacted) == {
        "items": [{"data": "[REDACTED]"}, {"nested": {"stringData": "[REDACTED]"}}]
    }
    assert "secret-value" not in redact_secret_data(
        "items:\n  - data:\n      token: secret-value\n"
    )


@pytest.mark.django_db
def test_kubectl_rejects_disabled_write_approval(tmp_path):
    with pytest.raises(ValueError, match="always need approval"):
        kubectl_bot(
            tmp_path,
            config="""\
clusters:
  configured:
    kubeconfig: /mounted/kubeconfig
approve: false""",
        )


@pytest.mark.django_db
@pytest.mark.parametrize(
    "args",
    [
        ["apply", "-f", "workload.yaml"],
        ["patch", "deployment", "example", "--type=merge", "-p", "{}"],
        ["delete", "pod", "example"],
        ["scale", "deployment", "example", "--replicas=2"],
        ["rollout", "pause", "deployment/example"],
        ["label", "pod", "example", "team=platform"],
        ["annotate", "pod", "example", "owner=lee"],
        ["create", "configmap", "example", "--from-literal=key=value"],
    ],
)
def test_kubectl_supported_writes_get_server_dry_run_previews(
    tmp_path, kubectl_calls, args
):
    _, _, plugin = kubectl_bot(tmp_path)

    preview = plugin.preview("configured", args)

    assert not preview.is_error
    assert "Server-side dry-run (exit 0)" in preview.text
    assert kubectl_calls[0][1 : len(args) + 1] == args
    assert "--dry-run=server" in kubectl_calls[0]
    assert kubectl_calls[0][-4:] == [
        "--kubeconfig",
        "/mounted/kubeconfig",
        "--namespace",
        "default",
    ]
    if args[0] in {"apply", "patch"}:
        assert len(kubectl_calls) == 2
        assert kubectl_calls[1][1] == "diff"
        assert kubectl_calls[1][2 : len(args) + 1] == args[1:]
        assert "kubectl diff (exit 0)" in preview.text
    else:
        assert len(kubectl_calls) == 1


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("args", "reason"),
    [
        (["exec", "pod/example", "--", "touch", "/tmp/changed"], "kubectl exec"),
        (["rollout", "restart", "deployment/example"], "rollout restart"),
        (["rollout", "undo", "deployment/example"], "rollout undo"),
        (["rollout", "status", "deployment/example"], "rollout status"),
    ],
)
def test_kubectl_unsupported_previews_explain_the_skip(
    tmp_path, kubectl_calls, args, reason
):
    _, _, plugin = kubectl_bot(tmp_path)

    preview = plugin.preview("configured", args)

    assert not preview.is_error
    assert preview.text.startswith("Preview skipped:")
    assert reason in preview.text
    assert kubectl_calls == []


@pytest.mark.django_db
def test_kubectl_preview_redacts_and_bounds_output(
    tmp_path, kubectl_calls, monkeypatch
):
    _, _, plugin = kubectl_bot(tmp_path)

    def fake_run(argv, **kwargs):
        kubectl_calls.append(argv)
        return subprocess.CompletedProcess(
            argv,
            0,
            stdout=(
                "data:\n  token: secret-value\npassword: another-secret\n"
                "Authorization: Bearer credential-value\n" + "x" * 4_000
            ),
            stderr="",
        )

    monkeypatch.setattr("django_ergo.plugins.kubectl.subprocess.run", fake_run)

    preview = plugin.preview("configured", ["delete", "pod", "example"])

    assert len(preview.text) <= 3_000
    assert "[... " in preview.text
    assert "secret-value" not in preview.text
    assert "another-secret" not in preview.text
    assert "credential-value" not in preview.text


@pytest.mark.django_db(transaction=True)
def test_kubectl_preview_error_blocks_approved_write(
    tmp_path, kubectl_calls, monkeypatch
):
    from asgiref.sync import async_to_sync

    def fake_run(argv, **kwargs):
        kubectl_calls.append(argv)
        if "--dry-run=server" not in argv:
            pytest.fail("an approved write ran after its preview failed")
        return subprocess.CompletedProcess(
            argv,
            1,
            stdout="data:\n  token: secret-value\n",
            stderr="Authorization: Bearer credential-value",
        )

    monkeypatch.setattr("django_ergo.plugins.kubectl.subprocess.run", fake_run)
    bot, _, _ = kubectl_bot(
        tmp_path,
        claude_tool(
            "kubectl_run",
            {"cluster": "configured", "args": ["delete", "pod", "example"]},
            tool_id="w1",
        ),
        say("Preview failed."),
    )
    user = User.objects.create(username="preview-error")
    root = async_to_sync(bot.root_session)(user)

    paused = async_to_sync(bot.ask)(root, "Remove the example pod")

    approval = paused.approvals[0]
    assert approval.preview_error
    assert "Preview failed" in approval.preview
    assert "secret-value" not in approval.preview
    assert "credential-value" not in approval.preview
    assert paused.call.metadata["pending_approvals"][0]["preview"] == approval.preview
    done = async_to_sync(bot.resume)(root, {"w1": True})
    assert done.text == "Preview failed."
    assert len(kubectl_calls) == 1


@pytest.mark.django_db(transaction=True)
def test_kubectl_run_waits_for_each_approval_and_shows_preview(tmp_path, kubectl_calls):
    from asgiref.sync import async_to_sync

    bot, engine, _ = kubectl_bot(
        tmp_path,
        claude_tool(
            "kubectl_read",
            {"cluster": "configured", "args": ["get", "pods"]},
            tool_id="r1",
        ),
        claude_tool(
            "kubectl_run",
            {"cluster": "configured", "args": ["delete", "pod", "example"]},
            tool_id="w1",
        ),
        say("Deleted."),
    )
    user = User.objects.create(username="kube-user")
    root = async_to_sync(bot.root_session)(user)

    paused = async_to_sync(bot.ask)(root, "Remove the example pod")
    assert [approval.tool_name for approval in paused.approvals] == ["kubectl_run"]
    assert "Server-side dry-run" in paused.approvals[0].preview
    assert kubectl_calls[-1][1:4] == ["delete", "pod", "example"]
    assert "--dry-run=server" in kubectl_calls[-1]
    assert kubectl_calls[-2][1:3] == ["get", "pods"]
    assert "Kubernetes" in _turn_context(engine._client.calls[0])

    done = async_to_sync(bot.resume)(root, {"w1": True})
    assert done.text == "Deleted."
    assert kubectl_calls[-1][1:4] == ["delete", "pod", "example"]


# ---------------------------------------------------------------------------
# bash
# ---------------------------------------------------------------------------


def bash_bot(tmp_path, *responses, config="cwd: /tmp"):
    yaml_text = f"""
        name: ops
        chats: {{main: {{skills: [bash]}}}}
        plugins: [{{name: bash, {config}}}]
    """
    bot, engine = make_bot(tmp_path, *responses, yaml_text=yaml_text, name="ops")
    return bot, engine, bot.plugin("bash")


@pytest.mark.django_db
def test_bash_runs_commands_with_exit_code_and_output(tmp_path):
    _, _, plugin = bash_bot(tmp_path, config=f"cwd: {tmp_path}")
    assert plugin.run("pwd") == f"Exit 0\n{tmp_path}"
    assert plugin.run("echo oops >&2; exit 3") == "Exit 3\noops"
    assert plugin.run("ls", cwd=str(tmp_path / "missing")).startswith(
        "No such directory"
    )
    long = plugin.run("seq 1 20000")
    assert "characters skipped" in long
    assert long.endswith("20000")


@pytest.mark.django_db(transaction=True)
async def test_bash_waits_for_approval(tmp_path):
    marker = tmp_path / "ran"
    bot, engine, _ = bash_bot(
        tmp_path,
        claude_tool("ergo_bash_run", {"command": f"touch {marker}"}, tool_id="b1"),
        say("Done."),
    )
    user = await User.objects.acreate(username="ops-user")
    root = await bot.root_session(user)
    paused = await bot.ask(root, "Touch the marker")
    assert [a.tool_name for a in paused.approvals] == ["ergo_bash_run"]
    assert not marker.exists()
    assert "ergo_bash_run runs bash commands" in _turn_context(engine._client.calls[0])
    done = await bot.resume(root, {"b1": True})
    assert done.text == "Done."
    assert marker.exists()


# ---------------------------------------------------------------------------
# browser
# ---------------------------------------------------------------------------

BROWSER_PAGE = (
    'data:text/html,<title>Search</title><form onsubmit="document.title='
    "'sent '%2Bq.value;return false\"><label>Query <input id=q></label>"
    "<button>Go</button></form>"
)


def browser_bot(tmp_path, *responses, config=""):
    yaml_text = f"""
        name: surfer
        chats: {{main: {{skills: [browser]}}}}
        plugins: [{{name: browser, {config}}}]
    """
    bot, engine = make_bot(tmp_path, *responses, yaml_text=yaml_text, name="surfer")
    return bot, engine, bot.plugin("browser")


def _chrome_binary():
    import os
    import shutil
    from pathlib import Path

    if os.environ.get("CHROME_BIN"):
        return os.environ["CHROME_BIN"]
    for name in ("google-chrome", "chromium", "chromium-browser"):
        if found := shutil.which(name):
            return found
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            path = p.chromium.executable_path
    except Exception:  # noqa: BLE001 — no Playwright or no bundled browser
        return None
    return path if Path(path).exists() else None


@pytest.fixture
def chrome(tmp_path):
    """A headless Chrome with remote debugging, like the one a person runs."""
    import socket
    import time
    import urllib.request

    pytest.importorskip("playwright")
    binary = _chrome_binary()
    if not binary:
        pytest.skip("no Chrome to drive")
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    proc = subprocess.Popen(
        [
            binary,
            "--headless=new",
            "--no-sandbox",
            "--no-first-run",
            f"--remote-debugging-port={port}",
            f"--user-data-dir={tmp_path / 'chrome-profile'}",
            "about:blank",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    url = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            urllib.request.urlopen(f"{url}/json/version", timeout=1)
            break
        except OSError:
            time.sleep(0.1)
    else:
        proc.kill()
        pytest.skip("Chrome did not start")
    yield url
    proc.kill()
    proc.wait()


@pytest.mark.django_db(transaction=True)
async def test_browser_actions_wait_for_approval(tmp_path):
    bot, engine, _ = browser_bot(
        tmp_path,
        claude_tool("ergo_browser_click", {"target": "e3"}, tool_id="c1"),
        say("Skipped."),
        config="takeover: the Chrome window on the desk",
    )
    user = await User.objects.acreate(username="surfer-user")
    root = await bot.root_session(user)
    paused = await bot.ask(root, "Click it")
    assert [a.tool_name for a in paused.approvals] == ["ergo_browser_click"]
    context = _turn_context(engine._client.calls[0])
    assert "the Chrome window on the desk" in context
    assert "wait for the user's approval" in context


@pytest.mark.django_db
def test_browser_reports_an_unreachable_chrome(tmp_path):
    pytest.importorskip("playwright")
    _, _, plugin = browser_bot(
        tmp_path, config="cdp_url: http://127.0.0.1:9, timeout: 2"
    )
    with pytest.raises(RuntimeError, match="Can't reach the browser"):
        plugin.tabs(type("Ctx", (), {"session": None})())


@pytest.mark.django_db(transaction=True)
async def test_browser_opens_types_and_screenshots(tmp_path, settings, chrome):
    settings.MEDIA_ROOT = str(tmp_path / "media")
    bot, engine, plugin = browser_bot(
        tmp_path,
        claude_tool("ergo_browser_open", {"url": BROWSER_PAGE}, tool_id="o1"),
        claude_tool(
            "ergo_browser_type",
            {"target": "e4", "text": "tacos", "submit": True},
            tool_id="t1",
        ),
        claude_tool("ergo_browser_screenshot", {}, tool_id="s1"),
        claude_tool("ergo_browser_tabs", {}, tool_id="l1"),
        say("Searched."),
        config=f"cdp_url: {chrome}, approve_actions: false",
    )
    user = await User.objects.acreate(username="surfer-user")
    root = await bot.root_session(user)
    done = await bot.ask(root, "Search for tacos")
    assert done.text == "Searched."

    results = [
        json.loads(block["content"])
        for message in engine._client.calls[-1]["messages"]
        if isinstance(message.get("content"), list)
        for block in message["content"]
        if block.get("type") == "tool_result"
    ]
    opened = results[0]
    assert opened["title"] == "Search"
    assert 'textbox "Query" [ref=e4]' in opened["snapshot"]
    # A new connection resolves the ref against a fresh snapshot.
    assert results[1]["title"] == "sent tacos"
    assert results[2]["filename"].startswith("browser-")
    current = [tab for tab in results[3] if tab.get("current")]
    assert [tab["tab"] for tab in current] == [opened["tab"]]
    shot = await ConversationAttachment.objects.aget(id=results[2]["id"])
    assert shot.session_id == root.id
    assert plugin._current[str(root.pk)] == opened["tab"]


# ---------------------------------------------------------------------------
# attachments
# ---------------------------------------------------------------------------


def files_bot(tmp_path, *responses, config="max_bytes: 100"):
    yaml_text = f"""
        name: filer
        chats: {{main: {{skills: [attachments]}}}}
        plugins: [{{name: attachments, {config}}}]
    """
    bot, engine = make_bot(tmp_path, *responses, yaml_text=yaml_text, name="filer")
    return bot, engine, bot.plugin("attachments")


@pytest.mark.django_db(transaction=True)
async def test_bot_writes_and_reads_files_in_its_session(tmp_path, settings):
    from asgiref.sync import sync_to_async

    from django_ergo.conversation.attachments import save_session_file

    settings.MEDIA_ROOT = str(tmp_path / "media")
    bot, engine, _ = files_bot(
        tmp_path,
        claude_tool(
            "ergo_attachments_create",
            {"filename": "plan.md", "content": "# Plan\nTacos"},
        ),
        say("Saved."),
        claude_tool("ergo_attachments_list", {}),
        say("Listed."),
    )
    user = await User.objects.acreate(username="filer-user")
    root = await bot.root_session(user)
    await sync_to_async(save_session_file)(root, "notes.txt", b"bring napkins")

    await bot.ask(root, "Write a plan")
    rows = [r async for r in root.attachments.order_by("filename")]
    assert [(r.filename, r.source, r.message_sequence) for r in rows] == [
        ("notes.txt", "upload", None),
        ("plan.md", "bot", None),
    ]
    assert "Files in this chat" in _turn_context(engine._client.calls[0])
    assert "notes.txt (text/plain" in _turn_context(engine._client.calls[0])

    await bot.ask(root, "What files are there?")
    listed = engine._client.calls[-1]["messages"][-1]["content"][0]["content"]
    assert "plan.md" in listed
    assert "notes.txt" in listed


@pytest.mark.django_db
def test_attachments_access_rules(tmp_path, settings):
    from django_ergo.bots.tools import ToolContext
    from django_ergo.conversation.attachments import save_session_file

    settings.MEDIA_ROOT = str(tmp_path / "media")
    bot, _, plugin = files_bot(tmp_path)
    lee = User.objects.create(username="lee-files")
    other = User.objects.create(username="someone-else")
    mine = ConversationSession.objects.create(user=lee, bot_name="filer")
    older = ConversationSession.objects.create(user=lee, bot_name="kitchen")
    theirs = ConversationSession.objects.create(user=other, bot_name="filer")
    recipe = save_session_file(older, "recipe.md", b"# Soup")
    secret = save_session_file(theirs, "secret.txt", b"nope")
    photo = save_session_file(mine, "fridge.jpg", b"\xff\xd8jpeg")
    ctx = ToolContext(bot=bot, session=mine, user=lee)

    assert plugin.read(ctx, str(recipe.id)).endswith("# Soup")
    assert [f["filename"] for f in plugin.list_files(ctx, str(older.id))] == [
        "recipe.md"
    ]
    assert "image attachment: fridge.jpg" in plugin.read(ctx, str(photo.id))
    with pytest.raises(ValueError, match="No file"):
        plugin.read(ctx, str(secret.id))
    with pytest.raises(ValueError, match="No session"):
        plugin.list_files(ctx, str(theirs.id))
    with pytest.raises(ValueError, match="read-only"):
        plugin.update(ctx, str(recipe.id), "# Stew")
    with pytest.raises(ValueError, match="not a text file"):
        plugin.update(ctx, str(photo.id), "x")
    with pytest.raises(ValueError, match="too large"):
        plugin.create(ctx, "big.txt", "x" * 101)

    # Files in another of the user's chats open by filename with its session id.
    assert plugin.read(ctx, "recipe.md", str(older.id)).endswith("# Soup")
    with pytest.raises(ValueError, match="No file 'stew.md' in that chat"):
        plugin.read(ctx, "stew.md", str(older.id))
    with pytest.raises(ValueError, match="No session"):
        plugin.read(ctx, "secret.txt", str(theirs.id))

    made = plugin.create(ctx, "list.md", "- eggs")
    updated = plugin.update(ctx, made["id"], "- eggs\n- milk")
    assert updated["size"] == len("- eggs\n- milk")
    assert plugin.read(ctx, made["id"]).endswith("- eggs\n- milk")


@pytest.mark.django_db
def test_attachments_archive_and_unarchive(tmp_path, settings):
    from datetime import timedelta

    from django.utils import timezone

    from django_ergo.bots.tools import ToolContext
    from django_ergo.conversation.attachments import save_session_file
    from django_ergo.conversation.models import ConversationAttachment

    settings.MEDIA_ROOT = str(tmp_path / "media")
    bot, _, plugin = files_bot(tmp_path)
    lee = User.objects.create(username="lee-archive")
    mine = ConversationSession.objects.create(user=lee, bot_name="filer")
    elsewhere = ConversationSession.objects.create(user=lee, bot_name="kitchen")
    ctx = ToolContext(bot=bot, session=mine, user=lee)
    now = timezone.now()
    rows = {}
    for days, name in [(10, "old.md"), (5, "mid.md"), (1, "new.md"), (0, "now.md")]:
        row = save_session_file(mine, name, name.encode())
        ConversationAttachment.objects.filter(id=row.id).update(
            updated_at=now - timedelta(days=days)
        )
        rows[name] = str(row.id)
    other = save_session_file(elsewhere, "keep.md", b"x")

    def names(**kw):
        return [f["filename"] for f in plugin.list_files(ctx, **kw)]

    # Needs ids or all_files; ids must be files of this session.
    with pytest.raises(ValueError, match="all_files"):
        plugin.archive(ctx)
    with pytest.raises(ValueError, match="No file"):
        plugin.archive(ctx, [str(other.id)])
    with pytest.raises(ValueError, match="No file nope"):
        plugin.archive(ctx, ["nope"])

    done = plugin.archive(ctx, [rows["old.md"]])
    assert [f["filename"] for f in done["archived"]] == ["old.md"]
    assert done["archived"][0]["archived_at"]
    assert done["remaining"] == 3
    assert names() == ["now.md", "new.md", "mid.md"]
    assert names(include_archived=True) == ["now.md", "new.md", "mid.md", "old.md"]
    # Still readable by id, and its age is kept.
    assert plugin.read(ctx, rows["old.md"]).endswith("old.md")
    assert ConversationAttachment.objects.get(id=rows["old.md"]).updated_at < now

    # All, but only older than 2 days and never the latest one.
    done = plugin.archive(ctx, all_files=True, older_than_days=2, keep_latest=1)
    assert [f["filename"] for f in done["archived"]] == ["mid.md"]
    done = plugin.archive(ctx, all_files=True, keep_latest=1)
    assert [f["filename"] for f in done["archived"]] == ["new.md"]
    assert names() == ["now.md"]
    assert ConversationAttachment.objects.get(id=other.id).archived_at is None

    # Hidden from the context listing and the skill hint, with a note.
    text = plugin.context_sources(ctx, "hi")[0].text()
    assert "now.md" in text
    assert "old.md" not in text
    assert "3 archived files not listed" in text
    assert plugin.skill_hint(ctx) == "1 file in this chat"

    back = plugin.unarchive(ctx, [rows["old.md"], rows["now.md"]])
    assert [f["filename"] for f in back["unarchived"]] == ["old.md"]
    assert "archived_at" not in back["unarchived"][0]
    assert names() == ["now.md", "old.md"]
    with pytest.raises(ValueError, match="attachment_ids"):
        plugin.unarchive(ctx, [])


@pytest.mark.django_db(transaction=True)
async def test_bot_archives_its_files(tmp_path, settings):
    from asgiref.sync import sync_to_async

    from django_ergo.conversation.attachments import save_session_file

    settings.MEDIA_ROOT = str(tmp_path / "media")
    bot, engine, _ = files_bot(
        tmp_path,
        claude_tool("ergo_attachments_archive", {"all_files": True}),
        say("Cleared."),
        say("Nothing here."),
    )
    user = await User.objects.acreate(username="archiver")
    root = await bot.root_session(user)
    await sync_to_async(save_session_file)(root, "notes.txt", b"bring napkins")

    await bot.ask(root, "Clear out the old files")
    assert "notes.txt (text/plain" in _turn_context(engine._client.calls[0])
    row = await root.attachments.aget()
    assert row.archived_at is not None
    assert row.file  # still stored

    await bot.ask(root, "Any files?")
    system = _turn_context(engine._client.calls[-1])
    assert "notes.txt (text/plain" not in system
    assert "1 archived file not listed" in system


@pytest.mark.django_db(transaction=True)
async def test_bot_looks_at_an_uploaded_image(tmp_path, settings):
    import base64

    from asgiref.sync import sync_to_async

    from django_ergo.conversation.attachments import save_session_file
    from django_ergo.conversation.models import MessageBlock
    from django_ergo.conversation.models import StructuredCall

    settings.MEDIA_ROOT = str(tmp_path / "media")
    bot, engine, _ = files_bot(tmp_path, config="max_bytes: 100")
    user = await User.objects.acreate(username="looker")
    root = await bot.root_session(user)
    photo = await sync_to_async(save_session_file)(root, "fridge.png", b"\x89PNGfake")
    engine._client.responses = [
        claude_tool(
            "ergo_attachments_look",
            {"attachment_id": str(photo.id), "question": "Any eggs?"},
        ),
        say("You have eggs."),
    ]
    result = await bot.ask(root, "What's in my fridge photo?")
    assert result.text == "You have eggs."
    # No side call: the image comes back in the tool result itself.
    assert not await StructuredCall.objects.filter(kind="attachment_look").aexists()
    tool_result = engine._client.calls[1]["messages"][-1]["content"][0]
    assert tool_result["type"] == "tool_result"
    text, image = tool_result["content"]
    assert text["text"].startswith(f"fridge.png (image/png, id={photo.id})")
    assert "Any eggs?" in text["text"]
    assert image == {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": "image/png",
            "data": base64.b64encode(b"\x89PNGfake").decode(),
        },
    }
    # History keeps a reference to the file, not the bytes.
    block = await MessageBlock.objects.aget(
        block_type="tool_result", message__session=root, tool_result_for="toolu_1"
    )
    assert block.tool_result_content[1]["attachment_id"] == str(photo.id)
    assert "data" not in block.tool_result_content[1]


@pytest.mark.django_db(transaction=True)
async def test_bot_sees_every_image_it_looks_at_in_one_round(tmp_path, settings):
    """Four looks at once (two sent with a message, two a tool saved as bot files)
    all come back as images, not as ``[image omitted]``."""
    import io
    from types import SimpleNamespace

    from asgiref.sync import sync_to_async
    from PIL import Image

    from django_ergo.conversation.attachments import Attachment
    from django_ergo.conversation.attachments import save_session_file
    from tests.test_conversation_structured import _usage

    def png(size, mode):
        out = io.BytesIO()
        Image.new(mode, size, (10, 20, 30, 255)[: len(mode)]).save(out, "PNG")
        return out.getvalue()

    settings.MEDIA_ROOT = str(tmp_path / "media")
    bot, engine, _ = files_bot(tmp_path, config="max_bytes: 100")
    user = await User.objects.acreate(username="batch-looker")
    root = await bot.root_session(user)
    engine._client.responses = [say("Got them.")]
    await bot.ask(
        root,
        "Here are two screenshots",
        attachments=[
            Attachment(
                media_type="image/png", data=png((64, 48), "RGB"), filename="a.png"
            ),
            Attachment(
                media_type="image/png", data=png((64, 48), "RGB"), filename="b.png"
            ),
        ],
    )
    sent_with_message = [r async for r in root.attachments.order_by("position")]
    renders = [
        await sync_to_async(save_session_file)(
            root, name, png(size, "RGBA"), source="bot", metadata={"penpot": {}}
        )
        for name, size in (("Home.png", (1500, 2000)), ("Menu.png", (400, 300)))
    ]
    rows = [*renders, *sent_with_message]
    engine._client.responses = [
        SimpleNamespace(
            content=[
                SimpleNamespace(
                    type="tool_use",
                    id=f"toolu_{i}",
                    name="ergo_attachments_look",
                    input={"attachment_id": str(row.id)},
                )
                for i, row in enumerate(rows)
            ],
            stop_reason="tool_use",
            usage=_usage(),
        ),
        say("All four look right."),
    ]

    result = await bot.ask(root, "Compare all four")

    assert result.text == "All four look right."
    results = engine._client.calls[-1]["messages"][-1]["content"]
    assert [b["tool_use_id"] for b in results] == [f"toolu_{i}" for i in range(4)]
    for block, row in zip(results, rows, strict=True):
        text, image = block["content"]
        assert row.filename in text["text"]
        assert image["type"] == "image", image
        assert image["source"]["media_type"] == "image/png"


@pytest.mark.django_db(transaction=True)
async def test_bot_looks_at_a_pdf_in_a_side_call(tmp_path, settings):
    from asgiref.sync import sync_to_async

    from django_ergo.conversation.attachments import save_session_file
    from django_ergo.conversation.models import StructuredCall

    settings.MEDIA_ROOT = str(tmp_path / "media")
    bot, engine, _ = files_bot(tmp_path, config="max_bytes: 100")
    user = await User.objects.acreate(username="pdf-looker")
    root = await bot.root_session(user)
    receipt = await sync_to_async(save_session_file)(
        root, "receipt.pdf", b"%PDF-1.4 fake"
    )
    engine._client.responses = [
        claude_tool(
            "ergo_attachments_look",
            {"attachment_id": str(receipt.id), "question": "Total?"},
        ),
        claude_text("The total is $12."),
        say("It came to $12."),
    ]
    result = await bot.ask(root, "What did the receipt say?")
    assert result.text == "It came to $12."
    parts = engine._client.calls[1]["messages"][0]["content"]
    assert [p["type"] for p in parts] == ["document", "text"]
    assert parts[1]["text"] == "Total?"
    answer = engine._client.calls[2]["messages"][-1]["content"][0]["content"]
    assert answer == "receipt.pdf: The total is $12."
    looked = await StructuredCall.objects.aget(kind="attachment_look")
    assert looked.user_id == user.id
    assert looked.metadata["attachment"] == str(receipt.id)


@pytest.mark.django_db
def test_bot_management_review_helpers(bot_repo, monkeypatch):
    _, work = bot_repo
    _, _, plugin = management_bot(work, "propose_pr")
    git(work, "add", "-A")
    git(work, "commit", "-m", "add bot")
    git(work, "push")
    assert plugin.draft_diff() == ""
    assert not plugin.draft_dir.exists()  # looking doesn't start a draft
    plugin.write("bots/manager/agents.md", "Be kind.")
    assert "Be kind." in plugin.draft_diff()

    calls = []
    real_run = plugin.run

    def run(args, cwd=None):
        if args[0] == "gh":
            calls.append(args[:4])
            if args[1:3] == ["pr", "list"]:
                return json.dumps([{"number": 3, "title": "Add CTO"}])
            return "diff --git a/x b/x\n"
        return real_run(args, cwd)

    monkeypatch.setattr(plugin, "run", run)
    assert plugin.pull_requests() == [{"number": 3, "title": "Add CTO"}]
    assert plugin.pull_request_diff(3).startswith("diff --git")
    assert plugin.merge_pull_request(3) == "Merged #3."
    assert plugin.close_pull_request(4) == "Closed #4."
    assert [c[:3] for c in calls[1:]] == [
        ["gh", "pr", "diff"],
        ["gh", "pr", "merge"],
        ["gh", "pr", "close"],
    ]


@pytest.mark.django_db(transaction=True)
async def test_telegram_hears_about_delegated_replies_in_the_root_chat(
    tmp_path, settings
):
    from asgiref.sync import sync_to_async

    from django_ergo.bots import messaging
    from django_ergo.conversation.models import ThreadMessage

    settings.DJANGO_ERGO = {"THREAD_MESSAGE_RUNNER": "tests.test_bots.record_message"}
    bot, engine, plugin = telegram_bot(
        tmp_path, say("The meal thread says tacos."), say("Done.")
    )
    cook = await User.objects.acreate(username="cook")
    root = await bot.root_session(cook)
    thread = await bot.create_session(cook, parent=root, title="Meals")
    request = await ThreadMessage.objects.acreate(
        sender_session=root,
        recipient_session=thread,
        text="Plan dinner",
        status="answered",
    )
    reply = await ThreadMessage.objects.acreate(
        sender_session=thread,
        recipient_session=root,
        in_reply_to=request,
        text="Tacos",
        depth=1,
    )
    await sync_to_async(messaging.deliver)(str(reply.id))
    assert str(plugin.api.sent()[-1]["chat_id"]) == "111"
    assert plugin.api.sent()[-1]["text"] == "The meal thread says tacos."

    # A request answered back to another thread isn't sent to Telegram.
    before = len(plugin.api.sent())
    asked = await ThreadMessage.objects.acreate(
        sender_session=thread, recipient_session=root, text="Status?"
    )
    await sync_to_async(messaging.deliver)(str(asked.id))
    assert len(plugin.api.sent()) == before


@pytest.mark.django_db
def test_a_writable_kb_saves_commits_and_pushes(bot_repo):
    remote, work = bot_repo
    folder = work / "bots"
    folder.mkdir()
    bot, _ = make_bot(
        folder,
        yaml_text="""
            name: cook
            plugins: [{name: ergo_kb, path: kb, write: true}]
        """,
        name="cook",
    )
    (bot.definition.root_dir / "kb").mkdir()
    (bot.definition.root_dir / "kb" / "index.md").write_text("# Kitchen\n")
    git(work, "add", "-A")
    git(work, "commit", "-m", "add cook")
    git(work, "push")
    toolkit = bot.plugin("ergo_kb").make_toolkit(None)
    assert "ergo_kb_write" in toolkit.tools

    result = toolkit.execute_tool(
        "ergo_kb_write",
        {
            "path": "preferences/breakfast.md",
            "content": "# Breakfast\nChocolate Soylent.",
        },
    )
    assert result == "Created preferences/breakfast.md. Saved and pushed."
    assert "kb: created preferences/breakfast.md" in git(
        remote, "log", "--oneline", "main"
    )
    assert git(work, "status", "--porcelain").strip() == ""

    again = toolkit.execute_tool(
        "ergo_kb_write",
        {
            "path": "preferences/breakfast.md",
            "content": "# Breakfast\nChocolate Soylent.",
        },
    )
    assert again == "Updated preferences/breakfast.md. No change to save."
    for bad in ("../agents.md", "notes.txt", "/etc/passwd.md"):
        with pytest.raises(ValueError, match="must be a .md file inside"):
            toolkit.execute_tool("ergo_kb_write", {"path": bad, "content": "x"})


@pytest.mark.django_db
def test_kb_writing_is_off_by_default(tmp_path):
    bot, _ = make_bot(
        tmp_path,
        yaml_text="name: reader\nplugins: [{name: ergo_kb, path: kb}]\n",
        name="reader",
    )
    assert "ergo_kb_write" not in bot.plugin("ergo_kb").make_toolkit(None).tools


def test_orca_help_cannot_smuggle_a_command():
    from django_ergo.plugins.orca import is_read_only

    assert is_read_only(["orchestration", "worker-start", "--help"])
    assert is_read_only(["worktree", "ps"])
    for sneaky in (
        ["terminal", "send", "--terminal", "t1", "--text", "-h", "--enter"],
        ["terminal", "send", "--text", "--help"],
        ["terminal", "create", "--title", "-h"],
        ["orchestration", "worker-stop", "-h"],
    ):
        assert not is_read_only(sneaky), sneaky


@pytest.mark.django_db
def test_merge_main_discard_stashes_and_writes_need_approval(bot_repo):
    _, work = bot_repo
    _, _, plugin = management_bot(work, "merge_main")
    git(work, "add", "-A")
    git(work, "commit", "-m", "add bot")
    tools = {t.name: t for t in plugin._tools()}
    assert tools["ergo_config_repo_write"].requires_approval
    assert tools["ergo_config_repo_edit"].requires_approval
    assert tools["ergo_config_repo_discard"].requires_approval
    (work / "notes.txt").write_text("a person's work in progress")
    assert plugin.discard() == "Set the unpublished changes aside (git stash)."
    assert "discarded by manager" in git(work, "stash", "list")
    assert plugin.discard() == "Nothing to discard."


def _merge_on_remote(tmp_path, remote, path, text, message):
    """Someone else's change lands on the remote main (a PR merged)."""
    other = tmp_path / "other"
    if not other.exists():
        git(tmp_path, "clone", "-b", "main", str(remote), str(other))
    git(other, "pull", "origin", "main")
    (other / path).parent.mkdir(parents=True, exist_ok=True)
    (other / path).write_text(text)
    git(other, "add", "-A")
    git(other, "commit", "-m", message)
    git(other, "push", "origin", "main")


@pytest.mark.django_db
def test_a_clean_draft_follows_main_after_a_merge(bot_repo, tmp_path, monkeypatch):
    remote, work = bot_repo
    _, _, plugin = management_bot(work, "propose_pr")
    git(work, "add", "-A")
    git(work, "commit", "-m", "add bot")
    git(work, "push")
    monkeypatch.setattr("django_ergo.plugins.bot_management.PR_FETCH_SECONDS", 0)
    assert "(clean)" in plugin.status()  # the draft exists, made from main

    _merge_on_remote(tmp_path, remote, "bots/manager/skills/new.md", "hi", "merged PR")

    assert plugin.read("bots/manager/skills/new.md") == "hi"
    assert "merged PR" in plugin.status()
    assert plugin.diff() == "(no changes)"


@pytest.mark.django_db
def test_publish_rebases_a_draft_that_main_moved_past(bot_repo, tmp_path, monkeypatch):
    remote, work = bot_repo
    _, _, plugin = management_bot(work, "propose_pr")
    git(work, "add", "-A")
    git(work, "commit", "-m", "add bot")
    git(work, "push")
    real_run = plugin.run
    monkeypatch.setattr(
        plugin,
        "run",
        lambda args, cwd=None: (
            "https://github.com/acme/bots/pull/8\n"
            if args[0] == "gh"
            else real_run(args, cwd)
        ),
    )
    monkeypatch.setattr("django_ergo.plugins.bot_management.PR_FETCH_SECONDS", 0)
    plugin.write("bots/manager/agents.md", "Be kind.")
    _merge_on_remote(tmp_path, remote, "bots/manager/skills/new.md", "hi", "merged PR")

    assert "has 1 commit(s) this draft doesn't" in plugin.status()
    assert "Be kind." in plugin.diff()  # changes are never moved under the bot

    branch = plugin.publish("Kinder", title="Kinder").split(" from ")[1].split(";")[0]
    log = git(remote, "log", "--format=%s", branch).splitlines()
    assert log[:2] == ["Kinder", "merged PR"]
    files = git(remote, "diff", "--name-only", f"main...{branch}").split()
    assert files == ["bots/manager/agents.md"]


@pytest.mark.django_db
def test_publish_refuses_a_draft_that_conflicts_with_main(
    bot_repo, tmp_path, monkeypatch
):
    remote, work = bot_repo
    _, _, plugin = management_bot(work, "propose_pr")
    git(work, "add", "-A")
    git(work, "commit", "-m", "add bot")
    git(work, "push")
    plugin.write("bots/manager/agents.md", "Be kind.")
    _merge_on_remote(tmp_path, remote, "bots/manager/agents.md", "Be terse.", "x")

    with pytest.raises(
        ValueError, match="conflict with origin/main in bots/manager/agents.md"
    ):
        plugin.publish("Kinder", title="Kinder")
    assert "Be kind." in plugin.diff()  # kept, uncommitted, on the draft
    assert "bot/manager/draft" in git(work, "branch")


@pytest.mark.django_db
def test_a_failed_publish_keeps_the_draft(bot_repo, monkeypatch):
    _, work = bot_repo
    _, _, plugin = management_bot(work, "propose_pr")
    git(work, "add", "-A")
    git(work, "commit", "-m", "add bot")
    git(work, "push")
    real_run = plugin.run

    def run(args, cwd=None):
        if args[0] == "gh":
            msg = "gh: not logged in"
            raise ValueError(msg)
        return real_run(args, cwd)

    monkeypatch.setattr(plugin, "run", run)
    plugin.write("bots/manager/agents.md", "Be kind.")
    with pytest.raises(ValueError, match="not logged in"):
        plugin.publish("Kinder", title="Kinder")
    assert "Be kind." in plugin.diff()  # still there, uncommitted, to publish again
    assert "bot/manager/draft" in git(work, "branch")


@pytest.mark.django_db(transaction=True)
def test_orca_attach_copies_a_worktree_file_into_the_chat(
    tmp_path, monkeypatch, settings
):
    from asgiref.sync import async_to_sync

    from django_ergo.bots.tools import ToolContext

    settings.MEDIA_ROOT = str(tmp_path / "media")
    bot, _, plugin = orca_bot(
        tmp_path, config='environment: devbox, executable: orca-test, files_host: ""'
    )
    worktree = tmp_path / "wt"
    (worktree / "out").mkdir(parents=True)
    (worktree / "out" / "report.md").write_text("# Done")
    (worktree / ".env").write_text("TOKEN=x")
    (tmp_path / "outside.txt").write_text("nope")
    (worktree / "out" / "link.txt").symlink_to(tmp_path / "outside.txt")
    (worktree / "out" / "safe-name").symlink_to(worktree / ".env")
    monkeypatch.setattr(type(plugin), "worktree_path", lambda self, w: str(worktree))
    assert "orca_attach" in [tool.name for tool in plugin._tools()]

    user = User.objects.create(username="attach")
    session = async_to_sync(bot.main_session)(user)
    ctx = ToolContext(bot=bot, session=session, user=user)
    attached = plugin.attach(ctx, "wt", "out/report.md")
    assert (attached["filename"], attached["media_type"]) == (
        "report.md",
        "text/markdown",
    )
    row = session.attachments.get()
    assert row.file.read() == b"# Done"
    assert row.metadata["from_orca"]["path"].endswith("/out/report.md")

    for path, error in (
        ("../outside.txt", "outside the worktree"),
        (".env", "looks like a secret"),
        ("out/link.txt", "outside the worktree"),
        ("out/safe-name", "secret"),
        ("out", "not a file"),
        ("out/missing.md", "Couldn't read"),
    ):
        with pytest.raises(ValueError, match=error):
            plugin.attach(ctx, "wt", path)
    plugin.max_attach_bytes = 3
    with pytest.raises(ValueError, match="larger than 3 bytes"):
        plugin.attach(ctx, "wt", "out/report.md")


@pytest.mark.django_db(transaction=True)
def test_orca_upload_copies_chat_files_into_a_worktree_folder(
    tmp_path, monkeypatch, settings
):
    from asgiref.sync import async_to_sync

    from django_ergo.bots.tools import ToolContext
    from django_ergo.conversation.attachments import save_session_file

    settings.MEDIA_ROOT = str(tmp_path / "media")
    bot, _, plugin = orca_bot(
        tmp_path, config='environment: devbox, executable: orca-test, files_host: ""'
    )
    worktree = tmp_path / "wt"
    worktree.mkdir()
    (tmp_path / "elsewhere").mkdir()
    (worktree / "escape").symlink_to(tmp_path / "elsewhere")
    monkeypatch.setattr(type(plugin), "worktree_path", lambda self, w: str(worktree))
    tool = next(t for t in plugin._tools() if t.name == "orca_upload")
    assert tool.requires_approval

    user = User.objects.create(username="uploader")
    session = async_to_sync(bot.main_session)(user)
    design = ConversationSession.objects.create(user=user, bot_name="design")
    save_session_file(design, "home.png", b"\x89PNG home")
    save_session_file(design, "tree.json", b'{"boards": []}')
    save_session_file(session, "brief.md", b"# Build it")
    ctx = ToolContext(bot=bot, session=session, user=user)

    written = plugin.upload(
        ctx, "wt", ["home.png", "tree.json"], "design", str(design.id)
    )
    assert [(w["file"], w["size"]) for w in written] == [
        ("home.png", 9),
        ("tree.json", 14),
    ]
    assert (worktree / "design" / "home.png").read_bytes() == b"\x89PNG home"
    plugin.upload(
        ctx, "wt", ["brief.md"], "docs/spec"
    )  # this chat's files, a nested folder
    assert (worktree / "docs" / "spec" / "brief.md").read_text() == "# Build it"

    for files, folder, error in (
        (["home.png"], "../out", "outside the worktree"),
        (["home.png"], "escape", "outside the worktree"),
        (["nope.png"], "design", "No file 'nope.png'"),
    ):
        with pytest.raises(ValueError, match=error):
            plugin.upload(ctx, "wt", files, folder, str(design.id))
    with pytest.raises(ValueError, match="can't be written"):
        plugin.push(str(worktree), "design", ".env", b"x")
    assert not (tmp_path / "elsewhere" / "home.png").exists()


@pytest.mark.django_db(transaction=True)
def test_orca_screenshot_attaches_the_image(tmp_path, monkeypatch, settings):
    import base64

    from asgiref.sync import async_to_sync

    from django_ergo.bots.tools import ToolContext
    from django_ergo.plugins.orca import is_read_only

    settings.MEDIA_ROOT = str(tmp_path / "media")
    bot, _, plugin = orca_bot(tmp_path)
    calls = []
    png = b"\x89PNG\r\n\x1a\nfake"

    def fake_run(argv, **kwargs):
        calls.append(argv)
        if "--page" in argv and argv[argv.index("--page") + 1] == "gone":
            out = '{"ok": false, "error": {"message": "Screenshot timed out"}}'
        else:
            out = json.dumps(
                {
                    "ok": True,
                    "result": {"data": base64.b64encode(png).decode(), "format": "png"},
                }
            )
        return subprocess.CompletedProcess(argv, 0, stdout=out, stderr="")

    monkeypatch.setattr("django_ergo.plugins.orca.subprocess.run", fake_run)
    user = User.objects.create(username="shooter")
    session = async_to_sync(bot.main_session)(user)
    ctx = ToolContext(bot=bot, session=session, user=user)
    shot = plugin.screenshot(ctx, worktree="path:/w", page="p1")
    assert shot["filename"].startswith("screenshot-") and shot["filename"].endswith(
        ".png"
    )
    assert session.attachments.get().file.read() == png
    assert calls[-1][:7] == [
        "orca-test",
        "screenshot",
        "--format",
        "png",
        "--worktree",
        "path:/w",
        "--page",
    ]
    assert calls[-1][-3:] == ["--environment", "devbox", "--json"]
    with pytest.raises(ValueError, match="Screenshot timed out"):
        plugin.screenshot(ctx, page="gone")
    assert "orca_screenshot" in [tool.name for tool in plugin._tools()]
    assert (
        is_read_only(["tab", "list"])
        and is_read_only(["snapshot"])
        and not is_read_only(["click"])
    )


def _turn_context(call):
    return "\n".join(
        block.get("text", "")
        for message in call["messages"]
        if isinstance(message.get("content"), list)
        for block in message["content"]
        if block.get("type") == "text"
        and block.get("text", "").startswith("<turn-context>")
    )
