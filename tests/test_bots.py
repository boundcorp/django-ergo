"""Tests for bot definitions, tool modules, plugins and the bot runtime."""

from __future__ import annotations

import json
import textwrap

import pytest
from django.contrib.auth import get_user_model

from django_ergo.bots import bot_tool
from django_ergo.bots.definition import BotDefinition
from django_ergo.bots.definition import BotDefinitionError
from django_ergo.bots.plugins import BotPlugin
from django_ergo.bots.plugins import resolve_plugin_class
from django_ergo.bots.registry import BotRegistry
from django_ergo.bots.runtime import Bot
from django_ergo.bots.tools import FunctionToolkit
from django_ergo.bots.tools import ToolContext
from django_ergo.conversation.adapters import ClaudeToolAdapter
from django_ergo.conversation.adapters import OpenAIToolAdapter
from django_ergo.conversation.context import TextContextSource
from django_ergo.conversation.models import ConversationSession
from tests.test_conversation_structured import claude_engine
from tests.test_conversation_structured import claude_text
from tests.test_conversation_structured import claude_tool

User = get_user_model()

TOOLS = textwrap.dedent(
    '''
    from django_ergo.bots import bot_tool

    PANTRY = {"eggs": 4}

    @bot_tool(description="Count an item in the pantry")
    def pantry_count(item: str) -> dict:
        return {"item": item, "count": PANTRY.get(item, 0)}

    @bot_tool(requires_approval=True, takes_context=True)
    def add_to_list(ctx, item: str, qty: int = 1) -> str:
        """Add an item to the shopping list."""
        return f"{ctx.user.username} added {qty} {item}"

    def helper():
        return "not a tool"
    '''
)


def write_bot(tmp_path, yaml_text, name="kitchen", tools=TOOLS):
    folder = tmp_path / name
    (folder / "tools").mkdir(parents=True)
    (folder / "agents.md").write_text("You run the kitchen.")
    (folder / "tools" / "pantry.py").write_text(tools)
    (folder / "bot.yaml").write_text(textwrap.dedent(yaml_text))
    return folder


KITCHEN_YAML = """
    name: kitchen
    description: Kitchen manager
    engine: {type: claude, config: {model: claude-test}}
    root: {recent: 4, budget_tokens: 4000}
    sessions:
      allow_create: true
      default_compaction: {mode: context_size, config: {keep_recent: 6}}
    tools: [tools/pantry.py]
    plugins:
      - name: tests.test_bots:RecordingPlugin
        note: hello
    permissions: {call_bots: [sysadmin]}
"""


class RecordingPlugin(BotPlugin):
    name = "recording"

    def on_load(self):
        self.events = ["load"]

    def context_sources(self, ctx, message):
        return [TextContextSource("Plugin note", self.config["note"])]

    async def on_session_created(self, session):
        self.events.append(("created", session.metadata["bot_role"]))

    def before_turn(self, session, message):  # sync hooks work too
        self.events.append(("before", message))

    async def after_turn(self, session, message, result):
        self.events.append(("after", message, result.text, len(result.approvals)))

    async def on_session_closed(self, session):
        self.events.append(("closed", session.metadata["bot_role"]))


# ---------------------------------------------------------------------------
# Definitions
# ---------------------------------------------------------------------------


def test_definition_loads_folder(tmp_path):
    folder = write_bot(tmp_path, KITCHEN_YAML)
    definition = BotDefinition.load(folder)

    assert definition.name == "kitchen"
    assert definition.instructions == "You run the kitchen."
    assert definition.engine_type == "claude"
    assert definition.recent == 4
    assert definition.allow_create_sessions is True
    assert definition.default_compaction_mode == "context_size"
    assert definition.default_compaction_config == {"keep_recent": 6}
    assert definition.tool_files == [(folder / "tools" / "pantry.py").resolve()]
    assert definition.plugins[0].name == "tests.test_bots:RecordingPlugin"
    assert definition.plugins[0].config == {"note": "hello"}
    assert definition.call_bots == ["sysadmin"]


@pytest.mark.parametrize(
    ("yaml_text", "error"),
    [
        ("name: x\ntools: [../evil.py]\n", "outside the bot folder"),
        ("name: x\ntools: [agents.md]\n", "must be a .py file"),
        ("name: x\ninstructions: missing.md\n", "not found"),
        ("name: x\nsessions: {default_compaction: {mode: weekly}}\n", "compaction"),
        ("name: x\nplugins: [{config: 1}]\n", "Invalid plugin"),
        ("name: x\nengine: claude\n", "engine must be a mapping"),
        ("- a list\n", "must be a mapping"),
    ],
)
def test_definition_rejects_bad_config(tmp_path, yaml_text, error):
    folder = write_bot(tmp_path, yaml_text)
    with pytest.raises(BotDefinitionError, match=error):
        BotDefinition.load(folder)


def test_definition_defaults_and_inline_instructions():
    definition = BotDefinition.from_dict({"name": "bare", "instructions_text": "Hi"})
    assert definition.instructions == "Hi"
    assert definition.recent == 15
    assert definition.allow_create_sessions is False
    assert definition.default_compaction_mode == "stream"
    with pytest.raises(BotDefinitionError, match="needs a name"):
        BotDefinition.from_dict({})


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


def test_bot_tool_infers_schema_and_runs():
    @bot_tool
    def search(query: str, limit: int = 5, exact: bool = False) -> list:
        """Search things."""
        return [query] * limit

    @bot_tool(takes_context=True, requires_approval=True, name="whoami")
    def me(ctx) -> str:
        return ctx.user

    toolkit = FunctionToolkit.from_functions([search, me], ToolContext(user="lee"))

    schema = search.__bot_tool__.json_schema()
    assert schema["properties"] == {
        "query": {"type": "string"},
        "limit": {"type": "integer"},
        "exact": {"type": "boolean"},
    }
    assert schema["required"] == ["query"]
    assert toolkit.execute_tool("search", {"query": "a", "limit": 2}) == '["a", "a"]'
    assert toolkit.execute_tool("whoami", {}) == "lee"
    assert toolkit.requires_approval("whoami")
    assert not toolkit.requires_approval("search")

    claude = toolkit.get_tools_schema(ClaudeToolAdapter())
    assert claude[0]["description"] == "Search things."
    assert claude[0]["input_schema"]["required"] == ["query"]
    openai = toolkit.get_tools_schema(OpenAIToolAdapter())
    assert openai[1]["function"]["name"] == "whoami"

    with pytest.raises(ValueError, match="Duplicate"):
        FunctionToolkit([search.__bot_tool__, search.__bot_tool__])


def test_plugin_resolution():
    assert resolve_plugin_class("tests.test_bots:RecordingPlugin") is RecordingPlugin
    with pytest.raises(ValueError, match="Unknown bot plugin"):
        resolve_plugin_class("nope")
    with pytest.raises(ValueError, match="is not a BotPlugin"):
        resolve_plugin_class("tests.test_bots.write_bot")


# ---------------------------------------------------------------------------
# Runtime
# ---------------------------------------------------------------------------


def make_bot(tmp_path, *responses, yaml_text=KITCHEN_YAML, name="kitchen"):
    """A bot whose engines (one per turn, like production) share a fake client."""
    engine = claude_engine(*responses)

    def factory():
        fresh = claude_engine()
        fresh._client = engine._client
        return fresh

    bot = Bot.load(write_bot(tmp_path, yaml_text, name=name), engine_factory=factory)
    return bot, engine


@pytest.mark.django_db(transaction=True)
async def test_root_session_turn_uses_stream_context_tools_and_hooks(tmp_path):
    user = await User.objects.acreate(username="cook")
    bot, engine = make_bot(
        tmp_path,
        claude_tool("pantry_count", {"item": "eggs"}),
        claude_text("You have 4 eggs."),
    )
    plugin = bot.plugin("recording")

    root = await bot.root_session(user)
    assert await bot.root_session(user) == root  # one root per user
    assert root.bot_name == "kitchen"
    assert root.compaction_config == {"native_history": "turn"}
    assert root.system_prompt == "You run the kitchen."

    result = await bot.ask(root, "How many eggs?")

    assert result.text == "You have 4 eggs."
    first, second = engine._client.calls
    assert first["system"].startswith("You run the kitchen.\n\n<context>")
    assert "## Plugin note\nhello" in first["system"]
    tools = [t["name"] for t in first["tools"]]
    assert {"pantry_count", "add_to_list", "history_read"} <= set(tools)
    assert "helper" not in tools
    assert '"count": 4' in second["messages"][-1]["content"][0]["content"]
    assert plugin.events == [
        "load",
        ("created", "root"),
        ("before", "How many eggs?"),
        ("after", "How many eggs?", "You have 4 eggs.", 0),
    ]


@pytest.mark.django_db(transaction=True)
async def test_approval_tool_pauses_then_resumes_with_context(tmp_path):
    user = await User.objects.acreate(username="shopper")
    bot, engine = make_bot(
        tmp_path,
        claude_tool("add_to_list", {"item": "milk", "qty": 2}, tool_id="t1"),
        claude_text("Added."),
    )
    root = await bot.root_session(user)

    paused = await bot.ask(root, "We need milk")
    assert paused.needs_approval
    assert paused.approvals[0].tool_name == "add_to_list"
    assert len(engine._client.calls) == 1

    done = await bot.resume(root, {"t1": True})
    assert done.text == "Added."
    tool_result = engine._client.calls[-1]["messages"][-1]["content"][0]
    assert tool_result["content"] == "shopper added 2 milk"


@pytest.mark.django_db(transaction=True)
async def test_threads_use_default_compaction_and_root_reads_them(tmp_path):
    user = await User.objects.acreate(username="planner")
    bot, engine = make_bot(tmp_path)
    plugin = bot.plugin("recording")
    root = await bot.root_session(user)

    thread = await bot.create_session(user, parent=root, title="Meal plan")
    assert thread.parent_id == root.id
    assert thread.compaction_mode == "context_size"
    assert thread.compaction_config == {"keep_recent": 6}
    assert thread.metadata == {"title": "Meal plan", "bot_role": "thread"}
    other = await bot.create_session(user, compaction_mode="time")
    assert other.compaction_config == {}
    with pytest.raises(ValueError, match="compaction"):
        await bot.create_session(user, compaction_mode="weekly")

    engine._client.responses = [claude_text("Tacos.")]
    await bot.ask(thread, "Plan Tuesday")
    # A thread keeps full native history and has no recent-window context.
    assert "<context>" in engine._client.calls[-1]["system"]  # plugin note only
    assert "Recent messages" not in engine._client.calls[-1]["system"]

    engine._client.responses = [
        claude_tool("history_sources", {}),
        claude_text("Tuesday is tacos."),
    ]
    await bot.ask(root, "What did the plan say?")
    listing = engine._client.calls[-1]["messages"][-1]["content"][0]["content"]
    assert f"session:{thread.id}" in listing
    assert f"session:{root.id}" in listing

    await bot.close_session(thread)
    assert ("closed", "thread") in plugin.events
    assert (await ConversationSession.objects.aget(id=thread.id)).status == "completed"


@pytest.mark.django_db(transaction=True)
async def test_api_key_comes_from_env_and_is_not_stored(tmp_path, monkeypatch):
    yaml_text = """
        name: keyed
        engine: {type: claude, config: {model: m}, api_key_env: KEYED_BOT_KEY}
    """
    bot = Bot.load(write_bot(tmp_path, yaml_text, name="keyed"))
    with pytest.raises(RuntimeError, match="KEYED_BOT_KEY"):
        bot.engine_spec()

    monkeypatch.setenv("KEYED_BOT_KEY", "sk-secret")
    spec = bot.engine_spec()
    assert spec.config == {"model": "m", "api_key": "sk-secret"}

    user = await User.objects.acreate(username="keyed")
    root = await bot.root_session(user)
    assert "sk-secret" not in str(root.metadata)


def test_registry_discovers_bots(tmp_path):
    write_bot(tmp_path, "name: alpha\n", name="alpha")
    write_bot(tmp_path, "name: beta\n", name="beta")
    (tmp_path / "not-a-bot").mkdir()

    registry = BotRegistry.discover(tmp_path)

    assert [bot.name for bot in registry] == ["alpha", "beta"]
    assert "alpha" in registry
    assert registry.get("beta").registry is registry
    with pytest.raises(KeyError, match="gamma"):
        registry.get("gamma")
    with pytest.raises(ValueError, match="Duplicate"):
        registry.load(tmp_path / "alpha")


# ---------------------------------------------------------------------------
# Orchestrator tools
# ---------------------------------------------------------------------------


def _tool_names(call):
    return {t["name"] for t in call["tools"]}


def _last_tool_result(engine):
    return engine._client.calls[-1]["messages"][-1]["content"][0]["content"]


@pytest.mark.django_db(transaction=True)
async def test_root_creates_and_drives_threads(tmp_path):
    user = await User.objects.acreate(username="orchestrator")
    bot, engine = make_bot(
        tmp_path,
        claude_tool(
            "threads_create",
            {
                "title": "Meal plan",
                "message": "Plan Tuesday",
                "compaction_mode": "time",
            },
        ),
        claude_text("Tacos on Tuesday."),  # the thread's reply
        claude_text("I started a meal plan thread."),
    )
    root = await bot.root_session(user)

    result = await bot.ask(root, "Plan meals")

    assert result.text == "I started a meal plan thread."
    root_call, thread_call, _ = engine._client.calls
    assert {"threads_list", "threads_create", "threads_send", "threads_close"} <= (
        _tool_names(root_call)
    )
    assert "bots_call" in _tool_names(root_call)  # permissions.call_bots is set
    # The thread is a normal session: no orchestrator tools, no recent window.
    assert "threads_create" not in _tool_names(thread_call)
    assert thread_call["messages"][0]["content"][0]["text"] == "Plan Tuesday"
    created = json.loads(_last_tool_result(engine))
    assert created["reply"] == "Tacos on Tuesday."

    thread = await ConversationSession.objects.aget(id=created["thread_id"])
    assert thread.parent_id == root.id
    assert thread.compaction_mode == "time"
    assert thread.metadata["title"] == "Meal plan"

    engine._client.responses = [
        claude_tool(
            "threads_send", {"thread_id": str(thread.id), "message": "And Wed?"}
        ),
        claude_text("Soup on Wednesday."),
        claude_tool("threads_list", {}, tool_id="t2"),
        claude_tool("threads_close", {"thread_id": str(thread.id)}, tool_id="t3"),
        claude_tool(
            "threads_send", {"thread_id": str(thread.id), "message": "x"}, tool_id="t4"
        ),
        claude_tool(
            "threads_send", {"thread_id": "nope", "message": "x"}, tool_id="t5"
        ),
        claude_text("Done."),
    ]
    await bot.ask(root, "Wednesday too")
    calls = engine._client.calls
    # The thread kept its own native history across turns.
    assert [m["role"] for m in calls[4]["messages"]] == ["user", "assistant", "user"]
    results = [c["messages"][-1]["content"][0] for c in calls[5:]]
    assert results[0]["content"] == "Soup on Wednesday."
    listing = json.loads(results[1]["content"])
    assert listing[0]["title"] == "Meal plan"
    assert listing[0]["messages"] == 4
    assert results[2]["content"] == f"Closed thread {thread.id}"
    assert results[3]["is_error"] and "closed" in results[3]["content"]
    assert results[4]["is_error"] and "No thread nope" in results[4]["content"]


@pytest.mark.django_db(transaction=True)
async def test_thread_creation_needs_permission(tmp_path):
    user = await User.objects.acreate(username="no-threads")
    bot, engine = make_bot(
        tmp_path, claude_text("ok"), yaml_text="name: plain\n", name="plain"
    )
    root = await bot.root_session(user)
    await bot.ask(root, "hi")
    tools = _tool_names(engine._client.calls[0])
    assert "threads_create" not in tools
    assert "threads_list" in tools


@pytest.mark.django_db(transaction=True)
async def test_bots_call_reaches_permitted_bot(tmp_path):
    user = await User.objects.acreate(username="chief")
    chief, engine = make_bot(
        tmp_path,
        claude_tool("bots_call", {"bot": "sysadmin", "message": "Disk space?"}),
        claude_text("Disk is 40% full."),
        claude_text("The server is fine."),
        yaml_text="name: chief\npermissions: {call_bots: [sysadmin]}\n",
        name="chief",
    )
    sysadmin, _ = make_bot(tmp_path, yaml_text="name: sysadmin\n", name="sysadmin")
    sysadmin._engine_factory = chief._engine_factory
    registry = BotRegistry()
    registry.add(chief)
    registry.add(sysadmin)

    root = await chief.root_session(user)
    result = await chief.ask(root, "How is the server?")

    assert result.text == "The server is fine."
    assert "bots_call" in _tool_names(engine._client.calls[0])
    assert _last_tool_result(engine) == "Disk is 40% full."
    called = await sysadmin.sessions(user).aget()
    assert called.metadata["called_by"] == "chief"

    # A second call reuses the same session; unknown bots are refused.
    engine._client.responses = [
        claude_tool("bots_call", {"bot": "sysadmin", "message": "And memory?"}),
        claude_text("Memory is fine."),
        claude_tool("bots_call", {"bot": "kitchen", "message": "hi"}, tool_id="t2"),
        claude_text("ok"),
    ]
    await chief.ask(root, "Memory?")
    assert await sysadmin.sessions(user).acount() == 1
    refused = engine._client.calls[-1]["messages"][-1]["content"][0]
    assert refused["is_error"]
    assert "may not call 'kitchen'" in refused["content"]


def test_run_bots_check_command(tmp_path):
    from io import StringIO

    from django.core.management import call_command
    from django.core.management.base import CommandError

    write_bot(tmp_path, "name: alpha\n", name="alpha")
    write_bot(tmp_path, "name: beta\n", name="beta")
    out = StringIO()
    call_command("run_bots", str(tmp_path), "--check", stdout=out)
    assert out.getvalue().splitlines() == ["alpha: plugins []", "beta: plugins []"]

    with pytest.raises(CommandError, match="No bot at"):
        call_command("run_bots", str(tmp_path / "missing"), "--check")
    write_bot(tmp_path, "name: x\ntools: [../evil.py]\n", name="bad")
    with pytest.raises(CommandError, match="outside the bot folder"):
        call_command("run_bots", str(tmp_path / "bad"), "--check")
