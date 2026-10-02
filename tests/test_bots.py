"""Tests for bot definitions, tool modules, plugins and the bot runtime."""

from __future__ import annotations

import itertools
import json
import textwrap

import pytest
from asgiref.sync import sync_to_async
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


_reply_ids = itertools.count()


def say(text, suggestions=None, kind="message"):
    """A model response that answers with a ChatReply."""
    reply = {"type": kind, "text": text}
    if suggestions:
        reply["suggestions"] = suggestions
    return claude_tool("send_reply", reply, tool_id=f"reply_{next(_reply_ids)}")


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


def make_bot(tmp_path, *responses, yaml_text=KITCHEN_YAML, name="kitchen", tools=TOOLS):
    """A bot whose engines (one per turn, like production) share a fake client."""
    engine = claude_engine(*responses)

    def factory():
        fresh = claude_engine()
        fresh._client = engine._client
        return fresh

    bot = Bot.load(
        write_bot(tmp_path, yaml_text, name=name, tools=tools), engine_factory=factory
    )
    return bot, engine


@pytest.mark.django_db(transaction=True)
async def test_root_session_turn_uses_stream_context_tools_and_hooks(tmp_path):
    user = await User.objects.acreate(username="cook")
    bot, engine = make_bot(
        tmp_path,
        claude_tool("pantry_count", {"item": "eggs"}),
        say("You have 4 eggs."),
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
    assert {"pantry_count", "add_to_list", "ergo_chat_history_read"} <= set(tools)
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
        say("Added."),
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

    engine._client.responses = [say("Tacos.")]
    await bot.ask(thread, "Plan Tuesday")
    # A thread keeps full native history and has no recent-window context.
    assert "<context>" in engine._client.calls[-1]["system"]  # plugin note only
    assert "Recent messages" not in engine._client.calls[-1]["system"]

    engine._client.responses = [
        claude_tool("ergo_chat_history_sources", {}),
        say("Tuesday is tacos."),
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


SENT: list[str] = []


def record_message(message_id):
    """A THREAD_MESSAGE_RUNNER for tests: deliver by hand, in order."""
    SENT.append(message_id)


@pytest.fixture
def thread_messages(settings):
    from django.conf import settings as django_settings

    SENT.clear()
    settings.DJANGO_ERGO = {
        **getattr(django_settings, "DJANGO_ERGO", {}),
        "THREAD_MESSAGE_RUNNER": "tests.test_bots.record_message",
    }

    async def deliver_next(registry=None):
        from django_ergo.bots import messaging

        message_id = SENT.pop(0)
        await sync_to_async(messaging.deliver)(message_id, registry)
        return message_id

    return deliver_next


def _seeded(call):
    return [
        part["content"]
        for message in call["messages"]
        if isinstance(message["content"], list)
        for part in message["content"]
        if part.get("type") == "tool_result"
    ]


def _texts(call):
    """Every text part the model was sent in a call."""
    return [
        part["text"]
        for message in call["messages"]
        if isinstance(message["content"], list)
        for part in message["content"]
        if part.get("type") == "text"
    ] + [m["content"] for m in call["messages"] if isinstance(m["content"], str)]


@pytest.mark.django_db(transaction=True)
async def test_a_bot_delegates_to_a_new_thread_and_gets_the_reply(tmp_path, thread_messages):
    from django_ergo.conversation.models import ThreadMessage

    user = await User.objects.acreate(username="orchestrator")
    bot, engine = make_bot(
        tmp_path,
        claude_tool(
            "ergo_thread_send",
            {"thread": "new", "title": "Meal plan", "message": "Plan Tuesday"},
        ),
        say("I asked a meal plan thread."),
        say("Tacos on Tuesday."),  # the thread's turn
        say("The thread says tacos on Tuesday."),  # the root, on the reply
    )
    root = await bot.root_session(user)

    result = await bot.ask(root, "Plan meals")
    assert result.text == "I asked a meal plan thread."
    tools = _tool_names(engine._client.calls[0])
    assert {"ergo_thread_list", "ergo_thread_send", "ergo_thread_archive"} <= tools
    assert not {t for t in tools if t.startswith("threads_")}
    sent = json.loads(engine._client.calls[1]["messages"][-1]["content"][0]["content"])
    assert sent["sent_to"] == "kitchen · Meal plan"
    thread = await ConversationSession.objects.aget(id=sent["thread_id"])
    assert thread.parent_id == root.id

    # The thread answers in a turn of its own...
    await thread_messages()
    asked = engine._client.calls[2]["messages"][0]["content"][0]["text"]
    assert asked.startswith("[Message from kitchen · Chat")
    assert asked.endswith("Plan Tuesday")
    request = await ThreadMessage.objects.aget(recipient_session=thread)
    assert (request.status, request.reply_text) == ("answered", "Tacos on Tuesday.")

    # ...and the reply comes back to the root as a new turn, answered there.
    await thread_messages()
    back = engine._client.calls[3]["messages"][-1]["content"][0]["text"]
    assert back.startswith("[Reply from kitchen · Meal plan")
    assert back.endswith("Tacos on Tuesday.")
    reply = await ThreadMessage.objects.aget(recipient_session=root)
    assert reply.in_reply_to_id == request.id
    assert reply.status == "answered"
    assert SENT == []  # a reply is never answered back


@pytest.mark.django_db(transaction=True)
async def test_a_delegated_turn_waits_for_approval_before_replying(tmp_path, thread_messages):
    from django_ergo.conversation.models import ThreadMessage

    user = await User.objects.acreate(username="approver")
    bot, engine = make_bot(
        tmp_path,
        claude_tool("ergo_thread_send", {"thread": "new", "message": "Add milk"}),
        say("Asked."),
        claude_tool("add_to_list", {"item": "milk"}, tool_id="add1"),  # the thread pauses
        say("Added milk."),
    )
    root = await bot.root_session(user)
    await bot.ask(root, "Get milk on the list")
    await thread_messages()
    request = await ThreadMessage.objects.aget(in_reply_to__isnull=True)
    assert request.status == "waiting"
    assert SENT == []

    thread = await ConversationSession.objects.aget(id=request.recipient_session_id)
    await bot.resume(thread, True)
    await request.arefresh_from_db()
    assert (request.status, request.reply_text) == ("answered", "Added milk.")
    assert len(SENT) == 1  # the reply is on its way to the root


@pytest.mark.django_db(transaction=True)
async def test_threads_are_listed_targeted_and_archived(tmp_path, thread_messages):
    user = await User.objects.acreate(username="lister")
    bot, engine = make_bot(tmp_path, say("ok"))
    root = await bot.root_session(user)
    thread = await bot.create_session(user, parent=root, title="Groceries")
    engine._client.responses = [
        claude_tool("ergo_thread_list", {}, tool_id="l1"),
        claude_tool("ergo_thread_send", {"thread": str(thread.id), "message": "Eggs?"}, tool_id="s1"),
        claude_tool("ergo_thread_send", {"thread": "nope", "message": "x"}, tool_id="s2"),
        claude_tool("ergo_thread_archive", {"thread_id": str(thread.id)}, tool_id="a1"),
        claude_tool("ergo_thread_list", {}, tool_id="l2"),
        say("Done."),
    ]
    await bot.ask(root, "Tidy up")
    results = [c["messages"][-1]["content"][0] for c in engine._client.calls[1:6]]
    listing = json.loads(results[0]["content"])
    assert [(r["thread"], r["title"]) for r in listing] == [
        (str(thread.id), "kitchen · Groceries"),
        ("root", "kitchen · Chat"),
    ]
    assert listing[1]["you_are_here"] is True
    assert json.loads(results[1]["content"])["thread_id"] == str(thread.id)
    assert results[2]["is_error"] and "No thread nope" in results[2]["content"]
    assert results[3]["content"] == "Archived kitchen · Groceries"
    assert [r["thread"] for r in json.loads(results[4]["content"])] == ["root"]

    # A message to an archived thread reopens it.
    engine._client.responses = [say("Four eggs.")]
    await thread_messages()
    await thread.arefresh_from_db()
    assert thread.status == "active"


@pytest.mark.django_db(transaction=True)
async def test_thread_creation_needs_permission(tmp_path):
    user = await User.objects.acreate(username="no-threads")
    bot, engine = make_bot(
        tmp_path,
        claude_tool("ergo_thread_send", {"thread": "new", "message": "hi"}),
        say("ok"),
        yaml_text="name: plain\n",
        name="plain",
    )
    root = await bot.root_session(user)
    await bot.ask(root, "hi")
    refused = _last_tool_result(engine)
    assert "may not start threads" in refused
    assert await bot.sessions(user).acount() == 1


@pytest.mark.django_db(transaction=True)
async def test_messages_reach_permitted_bots_only(tmp_path, thread_messages):
    user = await User.objects.acreate(username="chief")
    chief, engine = make_bot(
        tmp_path,
        claude_tool("ergo_thread_send", {"bot": "sysadmin", "message": "Disk space?"}),
        claude_tool("ergo_thread_send", {"bot": "kitchen", "message": "hi"}, tool_id="t2"),
        say("Asked."),
        say("Disk is 40% full."),  # sysadmin's root, on the message
        say("The server is fine."),  # chief, on the reply
        yaml_text="name: chief\ndescription: Runs things\npermissions: {call_bots: [sysadmin]}\n",
        name="chief",
    )
    sysadmin, _ = make_bot(
        tmp_path, yaml_text="name: sysadmin\ndescription: Keeps servers up\n", name="sysadmin"
    )
    sysadmin._engine_factory = chief._engine_factory
    registry = BotRegistry()
    registry.add(chief)
    registry.add(sysadmin)

    root = await chief.root_session(user)
    await chief.ask(root, "How is the server?")
    first = engine._client.calls[0]
    assert any("- sysadmin: Keeps servers up" in text for text in _seeded(first))
    refused = engine._client.calls[2]["messages"][-1]["content"][0]
    assert refused["is_error"] and "may not message 'kitchen'" in refused["content"]

    await thread_messages(registry)  # sysadmin's root chat answers
    target = await sysadmin.root_session(user)
    assert target.id != root.id
    await thread_messages(registry)  # the reply reaches chief's root
    assert any(text.endswith("Disk is 40% full.") for text in _texts(engine._client.calls[-1]))


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


# ---------------------------------------------------------------------------
# ChatReply turns
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
async def test_bot_turn_is_a_chat_reply_structured_call(tmp_path):
    user = await User.objects.acreate(username="asker")
    bot, engine = make_bot(
        tmp_path,
        say("Tacos or soup?", suggestions=["Tacos", "Soup"], kind="question"),
        claude_text("Plain text is not allowed"),
        say("Tacos it is."),
    )
    root = await bot.root_session(user)

    question = await bot.ask(root, "Plan dinner")
    assert question.reply.is_question
    assert question.suggestions == ["Tacos", "Soup"]
    assert question.call.kind == "chat_reply"
    assert question.call.session_id == root.id
    first = engine._client.calls[0]
    assert "send_reply" in {t["name"] for t in first["tools"]}
    assert (
        "Every reply to the user goes through the send_reply tool" in (first["system"])
    )

    answer = await bot.ask(root, "Tacos")
    assert answer.text == "Tacos it is."
    # A plain-text answer was sent back for a proper reply.
    correction = engine._client.calls[2]["messages"][-1]["content"][0]["text"]
    assert "must call the send_reply tool" in correction
    # History keeps each reply as readable text, suggestions included.
    window = engine._client.calls[1]["system"]
    assert "Tacos or soup?" in window
    assert "Suggested replies: Tacos / Soup" in window


@pytest.mark.django_db(transaction=True)
async def test_orchestration_can_be_turned_off(tmp_path):
    user = await User.objects.acreate(username="solo")
    bot, engine = make_bot(
        tmp_path,
        say("ok"),
        yaml_text="name: solo\norchestration: false\nsessions: {allow_create: true}\n",
        name="solo",
    )
    root = await bot.root_session(user)
    await bot.ask(root, "hi")
    tools = _tool_names(engine._client.calls[0])
    assert not {t for t in tools if t.startswith("threads_")}
    assert "ergo_chat_history_read" in tools


CONTEXT_TOOLS = textwrap.dedent(
    """
    from django_ergo.bots import bot_context, bot_tool

    @bot_tool(takes_context=True)
    def whoami(ctx) -> str:
        return ctx.secret("PANTRY_KEY") or "none"

    @bot_context(title="Shopping list")
    def shopping_list(ctx, message):
        return f"- eggs (for {ctx.user.get_username()}, re: {message})"

    @bot_context
    def broken(ctx, message):
        raise RuntimeError("pantry is down")
    """
)


@pytest.mark.django_db(transaction=True)
async def test_bot_context_functions_secrets_and_current_time(tmp_path, monkeypatch):
    monkeypatch.setenv("PANTRY_KEY", "shared")
    monkeypatch.setenv("PANTRY_KEY__MS_COOK", "cooks-own")
    user = await User.objects.acreate(username="ms-cook")
    yaml_text = """
        name: kitchen
        timezone: America/Denver
        tools: [tools/pantry.py]
    """
    bot, engine = make_bot(
        tmp_path,
        claude_tool("whoami", {}),
        say("Eggs are on the list."),
        yaml_text=yaml_text,
        tools=CONTEXT_TOOLS,
    )
    root = await bot.root_session(user)

    await bot.ask(root, "what do we need?")

    system = engine._client.calls[0]["system"]
    assert "## Shopping list" in system
    assert "- eggs (for ms-cook, re: what do we need?)" in system
    assert "## Current time" in system
    assert "MST" in system or "MDT" in system
    assert "pantry is down" not in system  # a failing context gives no text
    result = engine._client.calls[1]["messages"][-1]["content"][0]["content"]
    assert result == "cooks-own"


def test_secret_falls_back_to_shared_value(monkeypatch):
    from django_ergo.bots.tools import ToolContext

    monkeypatch.setenv("TANDOOR_KEY", "shared")
    monkeypatch.delenv("TANDOOR_KEY__AUD", raising=False)
    user = User(username="aud")
    assert ToolContext(user=user).secret("TANDOOR_KEY") == "shared"
    assert ToolContext().secret("MISSING_KEY", "dflt") == "dflt"
    user.timezone = "Not/AZone"
    assert ToolContext(user=user).timezone.key == "UTC"


def test_current_time_can_be_switched_off(tmp_path):
    bot, _ = make_bot(
        tmp_path, yaml_text="name: kitchen\ncurrent_time: false\ntools: []\n"
    )
    assert bot.definition.current_time is False


def test_explicit_parameters_respect_an_empty_required_list():
    props = {"query": {"type": "string"}}

    @bot_tool(parameters=props, required=[])
    def optional(query=""):
        return query

    @bot_tool(parameters=props)
    def everything(query):
        return query

    assert optional.__bot_tool__.json_schema()["required"] == []
    assert everything.__bot_tool__.json_schema()["required"] == ["query"]


@pytest.mark.django_db(transaction=True)
async def test_skills_are_listed_up_front_and_loaded_on_demand(tmp_path):
    user = await User.objects.acreate(username="planner")
    bot, engine = make_bot(
        tmp_path,
        claude_tool("load_skill", {"name": "meal-planning"}),
        say("Here are five dinners."),
        say("Sure."),
    )
    skills = bot.definition.root_dir / "skills"
    (skills / "meal-planning").mkdir(parents=True)
    (skills / "meal-planning" / "SKILL.md").write_text(
        "---\nname: meal-planning\ndescription: Plan a week of dinners\n---\n"
        "Check the last 60 days of the meal plan first."
    )
    (skills / "shopping.md").write_text("# Shop by aisle\n\nGroup the list by aisle.")
    bot = Bot.load(bot.definition.root_dir, engine_factory=bot._engine_factory)
    assert [s.name for s in bot.skills] == ["meal-planning", "shopping"]
    assert bot.skills[1].description == "Shop by aisle"

    root = await bot.root_session(user)
    turn = await bot.ask(root, "Plan dinners")
    assert turn.text == "Here are five dinners."

    first = engine._client.calls[0]
    assert {"list_skills", "load_skill", "pantry_count"} <= _tool_names(first)
    seeded = [
        block
        for message in first["messages"]
        for block in (message["content"] if isinstance(message["content"], list) else [])
        if block.get("type") == "tool_result"
    ]
    assert len(seeded) == 1
    listing = str(seeded[0]["content"])
    assert "- meal-planning: Plan a week of dinners" in listing
    assert "- shopping: Shop by aisle" in listing
    assert "pantry_count" in listing
    assert "send_reply" not in listing

    loaded = engine._client.calls[1]["messages"][-1]["content"][0]["content"]
    assert "Check the last 60 days" in str(loaded)
    assert "load_skill" in turn.call.metadata["tools"]
    assert turn.call.metadata["seeded"] is True

    # The root only sends the current turn natively, so it is seeded each turn.
    again = await bot.ask(root, "Thanks")
    assert again.call.metadata["seeded"] is True
    results = [
        block
        for message in engine._client.calls[2]["messages"]
        for block in (message["content"] if isinstance(message["content"], list) else [])
        if block.get("type") == "tool_result" and "Skills (load one" in str(block["content"])
    ]
    assert len(results) == 1


def test_bots_without_a_skills_folder_have_no_skill_tools(tmp_path):
    bot, _ = make_bot(tmp_path)
    assert bot.skills == []
    assert bot.reply_spec([]).pre_seeds == []


@pytest.mark.django_db(transaction=True)
async def test_nested_bot_folders_make_sub_bots_the_parent_can_message(tmp_path, thread_messages):
    user = await User.objects.acreate(username="lee")
    parent = write_bot(tmp_path, "name: boundcorp\ndescription: Boundcorp\n", name="boundcorp")
    write_bot(parent, "name: kitchen\ndescription: Runs the kitchen\norchestration: false\n", name="kitchen")
    write_bot(parent / "kitchen", "name: pantry\n", name="pantry")
    (parent / "skills").mkdir()
    write_bot(parent / "skills", "name: notabot\n", name="ignored")
    engine = claude_engine(
        claude_tool("ergo_thread_send", {"bot": "kitchen", "message": "What's for dinner?"}),
        say("I asked the kitchen."),
        say("Tacos."),
        say("Kitchen says tacos."),
    )

    def factory():
        fresh = claude_engine()
        fresh._client = engine._client
        return fresh

    registry = BotRegistry.discover(parent, engine_factory=factory)
    assert [b.name for b in registry] == ["boundcorp", "kitchen", "pantry"]
    boundcorp, kitchen, pantry = (registry.get(n) for n in ("boundcorp", "kitchen", "pantry"))
    assert (boundcorp.parent_name, kitchen.parent_name, pantry.parent_name) == ("", "boundcorp", "kitchen")
    assert [b.name for b in registry.children(boundcorp)] == ["kitchen"]
    assert registry.may_call(boundcorp, "kitchen")
    assert not registry.may_call(boundcorp, "pantry")
    assert not registry.may_call(kitchen, "boundcorp")

    root = await boundcorp.root_session(user)
    result = await boundcorp.ask(root, "Dinner?")
    assert result.text == "I asked the kitchen."
    first = engine._client.calls[0]
    assert "ergo_thread_send" in _tool_names(first)
    # The bots it can reach are pre-seeded as an ergo_bot_list result.
    assert any("- kitchen: Runs the kitchen" in text for text in _seeded(first))
    assert "- kitchen: Runs the kitchen" not in first["system"]

    await thread_messages(registry)  # kitchen's root chat answers
    kitchen_root = await kitchen.sessions(user).aget()
    assert kitchen_root.metadata["bot_role"] == "root"
    # The kitchen bot (orchestration off) has no thread or bot tools.
    kitchen_tools = _tool_names(engine._client.calls[2])
    assert not {t for t in kitchen_tools if t.startswith(("ergo_thread", "ergo_bot"))}

    await thread_messages(registry)  # the reply reaches boundcorp
    assert any(text.endswith("Tacos.") for text in _texts(engine._client.calls[-1]))


@pytest.mark.django_db(transaction=True)
async def test_a_kb_folder_is_the_bots_knowledge_base(tmp_path):
    user = await User.objects.acreate(username="eater")
    bot, engine = make_bot(tmp_path, say("Soylent, as usual."))
    kb = bot.definition.root_dir / "kb"
    kb.mkdir()
    (kb / "index.md").write_text(
        "# Kitchen\n\nLee has a chocolate Soylent shake for breakfast most days."
    )
    (kb / "recipes.md").write_text("# Recipes\n\nTacos on Tuesday.")
    bot = Bot.load(bot.definition.root_dir, engine_factory=bot._engine_factory)
    assert "ergo_kb" in [p.name for p in bot.plugins]

    root = await bot.root_session(user)
    await bot.ask(root, "What do I eat for breakfast?")
    first = engine._client.calls[0]
    assert "Knowledge base: Kitchen" in first["system"]
    assert "chocolate Soylent shake" in first["system"]
    assert {"ergo_kb_search", "ergo_kb_read"} <= _tool_names(first)


@pytest.mark.django_db(transaction=True)
async def test_idle_threads_are_archived_and_reopen_on_a_message(tmp_path):
    from datetime import timedelta

    from django.utils import timezone

    from django_ergo.bots import archival
    from django_ergo.conversation.models import ThreadMessage

    user = await User.objects.acreate(username="tidy")
    bot, _ = make_bot(tmp_path, say("ok"))
    root = await bot.root_session(user)
    old = await bot.create_session(user, parent=root, title="Old")
    busy = await bot.create_session(user, parent=root, title="Busy")
    fresh = await bot.create_session(user, parent=root, title="Fresh")
    await ThreadMessage.objects.acreate(recipient_session=busy, text="still working", status="delivered")
    ago = timezone.now() - timedelta(days=8)
    await ConversationSession.objects.filter(id__in=[root.id, old.id, busy.id]).aupdate(updated_at=ago)

    archived = await sync_to_async(archival.archive_idle_threads)([bot])
    assert archived == [str(old.id)]
    await old.arefresh_from_db()
    assert old.status == "completed"
    assert old.metadata["archived_reason"] == "idle"
    for session in (root, busy, fresh):
        await session.arefresh_from_db()
        assert session.status == "active"

    assert await sync_to_async(archival.reopen)(old)
    await old.arefresh_from_db()
    assert old.status == "active"
    assert "archived_at" not in old.metadata

    bot.definition.archive_after_days = 0  # never
    await ConversationSession.objects.filter(id=old.id).aupdate(updated_at=ago)
    assert await sync_to_async(archival.archive_idle_threads)([bot]) == []


TASK_TOOLS = textwrap.dedent(
    """
    from django_ergo.bots import bot_task, bot_tool

    @bot_task
    def slow_sum(numbers: list) -> int:
        return sum(numbers)

    @bot_task(name="shout")
    async def loud(word: str) -> str:
        return word.upper()

    @bot_tool(takes_context=True)
    def add_up(ctx, a: int, b: int) -> int:
        return ctx.tasks.run(slow_sum, [a, b], timeout=10)
    """
)


@pytest.mark.django_db(transaction=True)
async def test_tools_run_bot_tasks_in_the_background(tmp_path):
    user = await User.objects.acreate(username="tasker")
    bot, engine = make_bot(
        tmp_path,
        claude_tool("add_up", {"a": 2, "b": 3}),
        say("5"),
        yaml_text="name: tasker\ntools: [tools/pantry.py]\n",
        name="tasker",
        tools=TASK_TOOLS,
    )
    assert sorted(bot.tasks) == ["shout", "slow_sum"]
    root = await bot.root_session(user)
    await bot.ask(root, "2 + 3?")
    assert _last_tool_result(engine) == "5"

    ctx = bot.tool_context(root)
    job = ctx.tasks.start("shout", "hi")
    assert await job == "HI"
    assert job.done()
    with pytest.raises(ValueError, match="not a @bot_task"):
        ctx.tasks.start(len, [])


def test_a_bot_task_runner_setting_takes_over(tmp_path, settings):
    from django_ergo.bots import background

    calls = []

    class Done(background.TaskHandle):
        def done(self):
            return True

        def wait(self, timeout=None):
            return "queued elsewhere"

    def runner(bot_name, task_name, args, kwargs):
        calls.append((bot_name, task_name, args, kwargs))
        return Done()

    settings.DJANGO_ERGO = {"BOT_TASK_RUNNER": runner}
    bot = Bot.load(write_bot(tmp_path, "name: tasker\ntools: [tools/pantry.py]\n", name="tasker", tools=TASK_TOOLS))
    assert ToolContext(bot=bot).tasks.run("slow_sum", [1, 2]) == "queued elsewhere"
    assert calls == [("tasker", "slow_sum", [[1, 2]], {})]
    assert background.execute("tasker", "slow_sum", [[1, 2]], {}) == 3
