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
    assert "## Knowledge base results for this message" in first["system"]
    assert "Article 1A: Tacos (for 'dinner ideas')" in first["system"]
    assert "kb_search" in {t["name"] for t in first["tools"]}
    assert FakeKB.searches == [{"query": "dinner ideas", "top_k": 3}]

    await bot.ask(thread, "and lunch?")
    assert "Knowledge base results" not in engine._client.calls[1]["system"]
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
    assert "for 'first'" in engine._client.calls[0]["system"]
    assert "prefetch failed: index offline" in engine._client.calls[1]["system"]


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


def management_bot(work, mode, *responses):
    yaml_text = f"""
        name: manager
        plugins: [{{name: bot_management, mode: {mode}}}]
    """
    folder = work / "bots"
    folder.mkdir()
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
        ),
        claude_tool("ergo_config_repo_publish", {"message": "Shorter instructions"}, tool_id="p1"),
        say("Published."),
    )
    git(work, "add", "-A")
    git(work, "commit", "-m", "add bot")
    git(work, "push")
    user = await User.objects.acreate(username="manager")
    root = await bot.root_session(user)

    paused = await bot.ask(root, "Make your instructions shorter")
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


@pytest.mark.django_db(transaction=True)
async def test_bot_management_tools_are_root_only(bot_repo):
    _, work = bot_repo
    bot, engine, _ = management_bot(work, "merge_main", say("a"), say("b"))
    user = await User.objects.acreate(username="root-only")
    root = await bot.root_session(user)
    thread = await bot.create_session(user, parent=root)
    await bot.ask(root, "hi")
    await bot.ask(thread, "hi")
    root_tools = {t["name"] for t in engine._client.calls[0]["tools"]}
    thread_tools = {t["name"] for t in engine._client.calls[1]["tools"]}
    assert "ergo_config_repo_publish" in root_tools
    assert "ergo_config_repo_publish" not in thread_tools


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
    assert content[0]["type"] == "image"
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
    buttons = prompt["reply_markup"]["inline_keyboard"][0]
    assert buttons[0]["callback_data"] == f"ok:{session.id}"

    callback = {
        "id": "cb1",
        "data": buttons[0]["callback_data"],
        "message": {"chat": {"id": 111}},
    }
    await plugin.handle_update({"update_id": 2, "callback_query": callback})

    assert ("answerCallbackQuery", {"callback_query_id": "cb1"}) in plugin.api.calls
    assert plugin.api.sent()[-1]["text"] == "Flour is on the way."
    result = engine._client.calls[-1]["messages"][-1]["content"][0]
    assert result["content"] == "ordered flour"

    # Pressing again finds nothing left to approve.
    await plugin.handle_update({"update_id": 3, "callback_query": callback})
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
    assert {"ergo_kb_search", "ergo_kb_read", "ergo_kb_list"} <= {t["name"] for t in first["tools"]}
    assert "## Knowledge base results for this message" in first["system"]
    assert "### Diet (preferences/diet.md)" in first["system"]
    read = engine._client.calls[1]["messages"][-1]["content"][0]["content"]
    assert read.startswith("# preferences/diet.md")
    assert "No cilantro, ever." in read

    # Nothing relevant: no KB section at all.
    await bot.ask(root, "hello")
    assert "Knowledge base results" not in engine._client.calls[2]["system"]


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

    await plugin.handle_update(update(1, chat_id=group, text="hi", **{"from": {"id": 111}}))
    await plugin.handle_update(update(2, chat_id=group, text="yo", **{"from": {"id": 222}}))
    await plugin.handle_update(update(3, chat_id=group, text="??", **{"from": {"id": 333}}))

    assert [m["chat_id"] for m in plugin.api.sent()] == [group, group]
    assert len(engine._client.calls) == 2
    owners = [s.user_id async for s in bot.sessions().order_by("created_at")]
    assert owners[0] == cook.id
    assert len(owners) == 2


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
    assert [part["type"] for part in content] == ["image", "image", "text"]
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
        missing = await client.post("/hooks/kitchen/nope/update/", body, content_type="application/json")
        assert missing.status_code == 404

        ok = await client.post(
            url,
            body,
            content_type="application/json",
            headers={"X-Telegram-Bot-Api-Secret-Token": plugin.webhook_secret},
        )
        assert ok.status_code == 200
        await plugin.drain()
        assert plugin.api.sent()[-1] == {"chat_id": 111, "text": "Hello from a webhook."}
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
    assert plugin.read(["worktree", "ps"]) == '{"ok": true}'
    assert orca_calls[-1] == [
        "orca-test", "worktree", "ps", "--environment", "devbox", "--json",
    ]
    plugin.read(["skills", "get", "orchestration"])
    assert "--json" not in orca_calls[-1]
    plugin.read(["orchestration", "worker-start", "--help"])
    assert len(orca_calls) == 3


@pytest.mark.django_db
def test_orca_read_refuses_changes_and_other_environments(tmp_path, orca_calls):
    _, _, plugin = orca_bot(tmp_path)
    assert "use orca_run" in plugin.read(["orchestration", "worker-start", "--task", "t1"])
    assert "use orca_run" in plugin.read(["terminal", "send", "--text", "hi"])
    with pytest.raises(ValueError, match="only manages the 'devbox'"):
        plugin.run(["status", "--environment=prod"])
    assert orca_calls == []


@pytest.mark.django_db(transaction=True)
async def test_orca_run_waits_for_approval(tmp_path, orca_calls):
    bot, engine, _ = orca_bot(
        tmp_path,
        claude_tool("orca_read", {"args": ["orchestration", "worker-list"]}, tool_id="r1"),
        claude_tool(
            "orca_run", {"args": ["orchestration", "worker-stop", "--dispatch", "d1"]},
            tool_id="w1",
        ),
        say("Stopped."),
    )
    user = await User.objects.acreate(username="cto-user")
    root = await bot.root_session(user)

    paused = await bot.ask(root, "Stop worker d1")
    assert [a.tool_name for a in paused.approvals] == ["orca_run"]
    assert [c[1:3] for c in orca_calls] == [["orchestration", "worker-list"]]
    assert "Orca" in engine._client.calls[0]["system"]

    done = await bot.resume(root, {"w1": True})
    assert done.text == "Stopped."
    assert orca_calls[-1][1:3] == ["orchestration", "worker-stop"]


@pytest.mark.django_db
def test_orca_reports_a_missing_cli(tmp_path):
    _, _, plugin = orca_bot(tmp_path, config="executable: no-such-orca-cli")
    assert "is not installed" in plugin.read(["status"])
