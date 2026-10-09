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
from django_ergo.conversation.models import MessageBlock
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
    chats:
      main: {skills: [orchestration, pantry]}
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
        ("name: x\ncolor: chartreuse\n", "color"),
        ("name: x\ncolor: '#12'\n", "color"),
        ("name: x\nicon: a whole sentence\n", "icon"),
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
    assert definition.default_compaction_mode == "context_size"
    assert definition.tool_results_in_context is None
    with pytest.raises(BotDefinitionError, match="needs a name"):
        BotDefinition.from_dict({})


def test_pins_with_titles_and_icons():
    definition = BotDefinition.from_dict(
        {
            "name": "k",
            "chats": {
                "main": {
                    "pins": [
                        "pages/a.jhtml",
                        {"path": "./pages/b.jhtml", "title": "Board", "icon": "📊"},
                    ]
                }
            },
        }
    )
    main = definition.chat("main")
    assert main.pins == ["pages/a.jhtml", "pages/b.jhtml"]
    assert main.pin_labels == {"pages/b.jhtml": {"title": "Board", "icon": "📊"}}
    with pytest.raises(BotDefinitionError, match="pins"):
        BotDefinition.from_dict(
            {"name": "k", "chats": {"main": {"pins": [{"title": "x"}]}}}
        )


def test_icon_and_color():
    definition = BotDefinition.from_dict({"name": "k", "icon": "🍳", "color": "Amber"})
    assert (definition.icon, definition.color) == ("🍳", "amber")
    assert BotDefinition.from_dict({"name": "k", "color": "#F59E0B"}).color == "#F59E0B"
    bare = BotDefinition.from_dict({"name": "k"})
    assert (bare.icon, bare.color) == ("", "")


def test_old_stream_compaction_mode_loads_as_rolling(tmp_path):
    # "stream" was the name of rolling compaction before the rename.
    folder = write_bot(
        tmp_path,
        "name: x\nthreads: {default_compaction: {mode: stream, config: {batch: 4}}}\n",
    )
    definition = BotDefinition.load(folder)
    assert definition.default_compaction_mode == "context_size"
    assert definition.default_compaction_config == {"batch": 4}
    from django_ergo.conversation.models import CompactionMode

    assert CompactionMode.STREAM is CompactionMode.ROLLING


def test_tool_results_in_context_reaches_the_engine(tmp_path):
    bot, _ = make_bot(
        tmp_path, yaml_text=KITCHEN_YAML + "    tool_results_in_context: 1\n"
    )
    assert bot.definition.tool_results_in_context == 1
    assert bot.make_engine().tool_results_in_context == 1
    with pytest.raises(BotDefinitionError, match="tool_results_in_context"):
        BotDefinition.from_dict({"name": "k", "tool_results_in_context": "lots"})


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
async def test_root_session_turn_uses_window_context_tools_and_hooks(tmp_path):
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
    assert root.compaction_config == {}
    assert root.compaction_mode == "context_size"
    assert root.system_prompt == "You run the kitchen."

    result = await bot.ask(root, "How many eggs?")

    assert result.text == "You have 4 eggs."
    first, second = engine._client.calls
    assert first["system"] == "You run the kitchen."
    assert "## Plugin note\nhello" in first["messages"][0]["content"][0]["text"]
    tools = [t["name"] for t in first["tools"]]
    assert {"pantry_count", "add_to_list", "ergo_chat_history_read"} <= set(tools)
    assert "helper" not in tools
    assert '"count": 4' in second["messages"][-1]["content"][0]["content"]
    assert plugin.events == [
        "load",
        ("created", "main"),
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
    # The resumed turn goes natively, not in the recent-messages block too.
    resumed = engine._client.calls[-1]
    assert resumed["messages"][0]["content"][1]["text"] == "We need milk"
    assert "We need milk" not in resumed["system"]


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
    legacy = await bot.create_session(user, compaction_mode="stream")
    assert legacy.compaction_mode == "context_size"
    with pytest.raises(ValueError, match="compaction"):
        await bot.create_session(user, compaction_mode="weekly")

    engine._client.responses = [say("Tacos.")]
    await bot.ask(thread, "Plan Tuesday")
    # A thread keeps full native history and has no recent-window context.
    assert "<context>" in engine._client.calls[-1]["messages"][0]["content"][0]["text"]
    assert "Recent messages" not in engine._client.calls[-1]["system"]

    engine._client.responses = [
        claude_tool("ergo_chat_history_sources", {}),
        say("Tuesday is tacos."),
    ]
    await bot.ask(root, "What did the plan say?")
    listing = engine._client.calls[-1]["messages"][-1]["content"][0]["content"]
    assert f"session:{thread.id}" in listing
    assert f"session:{root.id}" in listing

    # A thread reads the person's other chats with the bot too.
    engine._client.responses = [
        claude_tool("ergo_chat_history_sources", {}),
        say("Found it."),
    ]
    await bot.ask(thread, "What did we say in main?")
    listing = engine._client.calls[-1]["messages"][-1]["content"][0]["content"]
    assert f"session:{root.id}" in listing
    assert f"session:{other.id}" in listing

    # A pasted link opens any of the person's chats, with this bot or another; not someone else's.
    elsewhere = await ConversationSession.objects.acreate(
        user=user, bot_name="other-bot", engine_type="claude", transport_type="api"
    )
    stranger = await User.objects.acreate(username="stranger")
    theirs = await ConversationSession.objects.acreate(
        user=stranger, bot_name="kitchen", engine_type="claude", transport_type="api"
    )
    assert (
        await sync_to_async(bot.linked_source)(thread, str(elsewhere.id))
    ).session == elsewhere
    assert await sync_to_async(bot.linked_source)(thread, str(theirs.id)) is None

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
async def test_a_bot_delegates_to_a_new_thread_and_gets_the_reply(
    tmp_path, thread_messages
):
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
    asked = engine._client.calls[2]["messages"][0]["content"][1]["text"]
    assert asked.startswith("[Message from kitchen · Main")
    assert asked.endswith("Plan Tuesday")
    request = await ThreadMessage.objects.aget(recipient_session=thread)
    assert (request.status, request.reply_text) == ("answered", "Tacos on Tuesday.")
    stored = await thread.messages.aget(sequence=0)
    assert stored.author == {"kind": "bot", "ref": "kitchen", "display_name": "kitchen"}
    assert stored.provenance["kind"] == "message"
    assert stored.provenance["origin"]["session_id"] == str(root.id)
    assert (
        await stored.content_blocks.values_list("text", flat=True).aget()
        == "Plan Tuesday"
    )

    # ...and the reply comes back to the root as a new turn, answered there.
    await thread_messages()
    back = next(
        text
        for text in _texts(engine._client.calls[3])
        if text.startswith("[Reply from")
    )
    assert back.startswith("[Reply from kitchen · Meal plan")
    assert back.endswith("Tacos on Tuesday.")
    reply_row = await root.messages.filter(provenance__kind="reply").afirst()
    assert reply_row.author["kind"] == "bot"
    assert reply_row.provenance["origin"]["session_id"] == str(thread.id)
    assert reply_row.provenance["reply_to"] == str(request.id)
    reply = await ThreadMessage.objects.aget(recipient_session=root)
    assert reply.in_reply_to_id == request.id
    assert reply.status == "answered"
    assert SENT == []  # a reply is never answered back


@pytest.mark.django_db(transaction=True)
async def test_no_nudge_right_after_a_reply(tmp_path, thread_messages):
    user = await User.objects.acreate(username="nudger")
    bot, engine = make_bot(
        tmp_path,
        claude_tool("ergo_thread_send", {"thread": "new", "message": "Plan Tuesday"}),
        say("Asked."),
        say("Started the plan; tacos so far."),  # the thread's turn
    )
    root = await bot.root_session(user)
    await bot.ask(root, "Plan meals")
    await thread_messages()  # the thread answers
    thread_id = json.loads(
        engine._client.calls[1]["messages"][-1]["content"][0]["content"]
    )["thread_id"]
    full = "Now plan Wednesday too: " + "a vegetarian dinner with leftovers. " * 12
    engine._client.responses = [
        claude_tool(
            "ergo_thread_send",
            {"thread": thread_id, "message": "Please continue with the started work"},
            tool_id="n1",
        ),
        claude_tool(
            "ergo_thread_send", {"thread": thread_id, "message": full}, tool_id="n2"
        ),
        say("Tacos so far; Wednesday is next."),
    ]
    await thread_messages()  # the reply reaches the root, which tries to nudge
    nudge, follow_up = _tool_results(engine)[-2:]
    assert nudge["is_error"] and "just replied" in nudge["content"]
    assert json.loads(follow_up["content"])["thread_id"] == thread_id


@pytest.mark.django_db(transaction=True)
async def test_a_delegated_turn_waits_for_approval_before_replying(
    tmp_path, thread_messages
):
    from django_ergo.conversation.models import ThreadMessage

    user = await User.objects.acreate(username="approver")
    bot, engine = make_bot(
        tmp_path,
        claude_tool("ergo_thread_send", {"thread": "new", "message": "Add milk"}),
        say("Asked."),
        claude_tool(
            "add_to_list", {"item": "milk"}, tool_id="add1"
        ),  # the thread pauses
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
        # ergo_thread_archive is the old name of ergo_thread_resolve.
        claude_tool("ergo_thread_archive", {"thread_id": str(thread.id)}, tool_id="a1"),
        claude_tool("ergo_thread_list", {}, tool_id="l2"),
        claude_tool(
            "ergo_thread_send",
            {"thread": str(thread.id), "message": "Eggs?"},
            tool_id="s1",
        ),
        claude_tool(
            "ergo_thread_send", {"thread": "nope", "message": "x"}, tool_id="s2"
        ),
        say("Done."),
    ]
    await bot.ask(root, "Tidy up")
    results = [c["messages"][-1]["content"][0] for c in engine._client.calls[1:6]]
    listing = json.loads(results[0]["content"])
    assert [(r["thread"], r["title"]) for r in listing] == [
        (str(thread.id), "kitchen · Groceries"),
        ("main", "kitchen · Main"),
    ]
    assert listing[1]["you_are_here"] is True
    assert results[1]["content"].startswith("Resolved kitchen · Groceries")
    assert [r["thread"] for r in json.loads(results[2]["content"])] == ["main"]
    assert json.loads(results[3]["content"])["thread_id"] == str(thread.id)
    assert results[4]["is_error"] and "No thread nope" in results[4]["content"]

    # A message to a resolved thread reopens it.
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
        claude_tool(
            "ergo_thread_send", {"bot": "kitchen", "message": "hi"}, tool_id="t2"
        ),
        say("Asked."),
        say("Disk is 40% full."),  # sysadmin's root, on the message
        say("The server is fine."),  # chief, on the reply
        yaml_text="name: chief\ndescription: Runs things\npermissions: {call_bots: [sysadmin]}\n",
        name="chief",
    )
    sysadmin, _ = make_bot(
        tmp_path,
        yaml_text="name: sysadmin\ndescription: Keeps servers up\n",
        name="sysadmin",
    )
    sysadmin._engine_factory = chief._engine_factory
    registry = BotRegistry()
    registry.add(chief)
    registry.add(sysadmin)

    root = await chief.root_session(user)
    await chief.ask(root, "How is the server?")
    first = engine._client.calls[0]
    assert "### sysadmin: Keeps servers up" in _turn_context(first)
    refused = engine._client.calls[2]["messages"][-1]["content"][0]
    assert refused["is_error"] and "may not message 'kitchen'" in refused["content"]

    await thread_messages(registry)  # sysadmin's root chat answers
    target = await sysadmin.root_session(user)
    assert target.id != root.id
    await thread_messages(registry)  # the reply reaches chief's root
    assert any(
        text.endswith("Disk is 40% full.") for text in _texts(engine._client.calls[-1])
    )


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
        "Every reply to the user goes through the send_reply tool"
        in first["messages"][0]["content"][0]["text"]
    )

    answer = await bot.ask(root, "Tacos")
    assert answer.text == "Tacos it is."
    # A plain-text answer was sent back for a proper reply.
    # The turn context goes first in the newest user message, here the nudge.
    correction = engine._client.calls[2]["messages"][-1]["content"][1]["text"]
    assert "Call the send_reply tool now" in correction
    assert "Plain text is not allowed" in correction
    # The nudge is stored as Ergo's, not as the user's message.
    nudges = [
        m.author
        async for m in root.messages.filter(
            content_blocks__text__contains="Call the send_reply tool now"
        )
    ]
    assert [a.get("kind") for a in nudges] == ["system"]
    # History keeps each reply as readable text, suggestions included.
    window = str(engine._client.calls[1]["messages"])
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

    system = _turn_context(engine._client.calls[0])
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
async def test_skills_are_in_per_turn_context_and_load_on_demand(tmp_path):
    user = await User.objects.acreate(username="planner")
    bot, engine = make_bot(
        tmp_path,
        claude_tool("ergo_skill_load", {"name": "meal-planning"}),
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
    assert {"ergo_skills_list", "ergo_skill_load", "pantry_count"} <= _tool_names(first)
    context = _turn_context(first)
    assert "## Skills" in context
    assert "- meal-planning (not loaded): Plan a week of dinners" in context
    assert "- shopping (not loaded): Shop by aisle" in context
    assert "- pantry (loaded): Tools from pantry.py" in context
    assert _seeded(first) == []
    assert "seeded" not in turn.call.metadata
    assert not await MessageBlock.objects.filter(
        message__session=root,
        tool_name="ergo_skills_list",
        tool_use_id__startswith="preseed_",
    ).aexists()

    loaded = engine._client.calls[1]["messages"][-1]["content"][0]["content"]
    assert "Check the last 60 days" in str(loaded)
    assert "ergo_skill_load" in turn.call.metadata["tools"]

    again = await bot.ask(root, "Thanks")
    assert "seeded" not in again.call.metadata
    # Native history keeps the loaded skill, but nothing was pre-seeded.
    assert not any(
        part.get("tool_use_id", "").startswith("preseed_")
        for message in engine._client.calls[2]["messages"]
        if isinstance(message["content"], list)
        for part in message["content"]
    )


@pytest.mark.django_db(transaction=True)
async def test_user_defined_seeded_tool_seeds_once_and_stays_in_history(tmp_path):
    tools = TOOLS + textwrap.dedent(
        """
        @bot_tool(seed=True)
        def account_limits() -> str:
            '''Current account limits.'''
            return "10 left"
        """
    )
    user = await User.objects.acreate(username="planner")
    bot, engine = make_bot(tmp_path, say("First."), say("Second."), tools=tools)
    root = await bot.root_session(user)

    first = await bot.ask(root, "First")
    second = await bot.ask(root, "Second")

    # Main chats keep native history, so the first seed is still there.
    assert first.call.metadata["seeded"]
    assert "seeded" not in second.call.metadata
    assert [
        [r for r in _seeded(call) if r == "10 left"] for call in engine._client.calls
    ] == [["10 left"], ["10 left"]]


def test_bots_without_a_skills_folder_have_no_skill_tools(tmp_path):
    bot, _ = make_bot(tmp_path)
    assert bot.skills == []
    assert bot.reply_spec([]).pre_seeds == []


@pytest.mark.django_db(transaction=True)
async def test_nested_bot_folders_make_sub_bots_the_parent_can_message(
    tmp_path, thread_messages
):
    user = await User.objects.acreate(username="lee")
    parent = write_bot(
        tmp_path, "name: boundcorp\ndescription: Boundcorp\n", name="boundcorp"
    )
    write_bot(
        parent,
        "name: kitchen\ndescription: Runs the kitchen\norchestration: false\n",
        name="kitchen",
    )
    write_bot(parent / "kitchen", "name: pantry\n", name="pantry")
    (parent / "skills").mkdir()
    write_bot(parent / "skills", "name: notabot\n", name="ignored")
    engine = claude_engine(
        claude_tool(
            "ergo_thread_send", {"bot": "kitchen", "message": "What's for dinner?"}
        ),
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
    boundcorp, kitchen, pantry = (
        registry.get(n) for n in ("boundcorp", "kitchen", "pantry")
    )
    assert (boundcorp.parent_name, kitchen.parent_name, pantry.parent_name) == (
        "",
        "boundcorp",
        "kitchen",
    )
    assert [b.name for b in registry.children(boundcorp)] == ["kitchen"]
    assert registry.may_call(boundcorp, "kitchen")
    assert not registry.may_call(boundcorp, "pantry")
    assert registry.may_call(kitchen, "boundcorp")  # upward: always
    assert not registry.may_call(pantry, "boundcorp")

    root = await boundcorp.root_session(user)
    result = await boundcorp.ask(root, "Dinner?")
    assert result.text == "I asked the kitchen."
    first = engine._client.calls[0]
    assert "ergo_thread_send" in _tool_names(first)
    # The bots it can reach are in the "Bots and threads" context block.
    assert "## Bots and threads\n### boundcorp (this bot)" in _turn_context(first)
    assert "### kitchen: Runs the kitchen" in _turn_context(first)
    assert not _seeded(first) or not any(
        "Bots you can message" in t for t in _seeded(first)
    )

    await thread_messages(registry)  # kitchen's root chat answers
    kitchen_root = await kitchen.sessions(user).aget()
    assert kitchen_root.metadata["bot_role"] == "main"
    # The kitchen bot (orchestration off) has no thread or bot tools.
    kitchen_tools = _tool_names(engine._client.calls[2])
    assert not {t for t in kitchen_tools if t.startswith(("ergo_thread", "ergo_bot"))}
    assert "ergo_message_up" in kitchen_tools  # but it can always message upward

    await thread_messages(registry)  # the reply reaches boundcorp
    assert any(text.endswith("Tacos.") for text in _texts(engine._client.calls[-1]))


def _family(tmp_path, engine, parent_yaml, child_yaml):
    """A parent bot with one sub-bot, sharing ``engine``'s scripted responses."""
    parent = write_bot(tmp_path, parent_yaml, name="boundcorp")
    write_bot(parent, child_yaml, name="design")

    def factory():
        fresh = claude_engine()
        fresh._client = engine._client
        return fresh

    registry = BotRegistry.discover(parent, engine_factory=factory)
    return registry, registry.get("boundcorp"), registry.get("design")


def _tool_results(engine):
    """Every tool result the model was sent, in order (the last call's messages)."""
    return [
        part
        for message in engine._client.calls[-1]["messages"]
        if isinstance(message["content"], list)
        for part in message["content"]
        if part.get("type") == "tool_result"
    ]


@pytest.mark.django_db(transaction=True)
async def test_a_bot_without_orchestration_can_always_message_upward(
    tmp_path, thread_messages
):
    from django_ergo.conversation.models import ThreadMessage

    user = await User.objects.acreate(username="upward")
    engine = claude_engine(
        claude_tool("ergo_message_up", {"to": "parent", "message": "Need a dev"}),
        say("Asked boundcorp."),
    )
    registry, boundcorp, design = _family(
        tmp_path,
        engine,
        "name: boundcorp\n",
        "name: design\norchestration: false\nthreads: {allow_create: true}\n",
    )
    design_main = await design.root_session(user)
    await design.ask(design_main, "Get this built")
    tools = _tool_names(engine._client.calls[0])
    assert "ergo_message_up" in tools
    assert not {t for t in tools if t.startswith(("ergo_thread", "ergo_bot"))}
    sent = await ThreadMessage.objects.select_related("recipient_session").aget()
    assert sent.recipient_session.bot_name == "boundcorp"
    assert sent.recipient_session.metadata["bot_role"] == "main"
    assert sent.metadata["message_author"] == {
        "kind": "bot",
        "ref": "design",
        "display_name": "design",
    }
    assert sent.metadata["message_provenance"]["kind"] == "report"

    engine._client.responses = [say("Noted.")]
    await thread_messages(registry)
    incoming = await sent.recipient_session.messages.aget(sequence=0)
    assert incoming.author == sent.metadata["message_author"]
    assert incoming.provenance == sent.metadata["message_provenance"]
    assert (
        await incoming.content_blocks.values_list("text", flat=True).aget()
        == "Need a dev"
    )

    # A design thread may message design's main chat, not the parent.
    thread = await design.create_session(user, parent=design_main, title="Logo")
    engine._client.responses = [
        claude_tool("ergo_message_up", {"to": "parent", "message": "x"}, tool_id="u1"),
        claude_tool("ergo_message_up", {"to": "main", "message": "Done"}, tool_id="u2"),
        say("Reported."),
    ]
    await design.ask(thread, "Finish up")
    refused, sent = _tool_results(engine)[-2:]
    assert refused["is_error"] and "Only the main chat" in refused["content"]
    assert json.loads(sent["content"])["sent_to"] == "design · Main"

    # The main chat itself has nowhere to go but up to the parent.
    root = await boundcorp.root_session(user)
    engine._client.responses = [say("ok")]
    await boundcorp.ask(root, "hi")
    assert "ergo_message_up" not in _tool_names(engine._client.calls[-1])


@pytest.mark.django_db(transaction=True)
async def test_upward_reaches_only_the_parents_main_chat(tmp_path, thread_messages):
    user = await User.objects.acreate(username="up-main")
    engine = claude_engine()
    registry, boundcorp, design = _family(
        tmp_path, engine, "name: boundcorp\n", "name: design\n"
    )
    root = await boundcorp.root_session(user)
    side = await boundcorp.create_session(user, parent=root, title="Side")
    design_main = await design.root_session(user)
    engine._client.responses = [
        claude_tool(
            "ergo_thread_send",
            {"bot": "boundcorp", "thread": str(side.id), "message": "x"},
            tool_id="s1",
        ),
        claude_tool(
            "ergo_thread_send", {"bot": "boundcorp", "message": "Status?"}, tool_id="s2"
        ),
        say("ok"),
    ]
    await design.ask(design_main, "Report")
    refused, sent = _tool_results(engine)[-2:]
    assert refused["is_error"] and "your parent bot" in refused["content"]
    assert json.loads(sent["content"])["sent_to"] == "boundcorp · Main"
    assert (
        "### boundcorp (your parent bot: message its main chat only)"
        in _turn_context(engine._client.calls[0])
    )


@pytest.mark.django_db(transaction=True)
async def test_orchestrators_see_every_bots_chats_and_latest_messages(
    tmp_path, thread_messages, monkeypatch
):
    from django_ergo.bots import overview
    from django_ergo.bots.tools import ToolContext

    user = await User.objects.acreate(username="overseer")
    engine = claude_engine()
    registry, boundcorp, design = _family(
        tmp_path,
        engine,
        "name: boundcorp\ndescription: Routes things\npull_requests: [boundcorp/ergo-bots]\n",
        "name: design\ndescription: Designs things\nthreads: {allow_create: true}\n",
    )
    monkeypatch.setattr(
        overview,
        "gh_open_prs",
        lambda repos: [f"- {r} #88 (draft): Restyle" for r in repos],
    )
    root = await boundcorp.root_session(user)
    design_main = await design.root_session(user)
    logo = await design.create_session(
        user,
        title="Logo",
        metadata={
            "started_by": str(root.id),
            "started_by_bot": "boundcorp",
            "started_by_label": "boundcorp · Main",
        },
    )
    engine._client.responses = [say("Here is the logo, in navy.")]
    await design.ask(logo, "Make a logo " + "with care " * 100)
    engine._client.responses = [say("Hi from design main.")]
    await design.ask(design_main, "Hello design")

    ctx = ToolContext(bot=boundcorp, session=root, user=user)
    text = await sync_to_async(overview.overview)(ctx)
    lines = text.splitlines()
    assert lines[0] == "### boundcorp (this bot): Routes things"
    assert "- main: idle, last just now, you are here" in lines
    assert "### design: Designs things" in lines
    logo_line = next(line for line in lines if line.startswith("- Logo (thread"))
    assert "started by boundcorp · Main" in logo_line
    # Snippets: oldest first, long messages clipped, the newest one with more room.
    at = lines.index(logo_line)
    assert lines[at + 1].startswith("    in: Make a logo with care")
    assert lines[at + 1].endswith("…") and len(lines[at + 1]) <= 4 + 4 + 250
    assert lines[at + 2] == "    reply: Here is the logo, in navy."
    assert "### Open pull requests" in lines
    assert "- boundcorp/ergo-bots #88 (draft): Restyle" in lines

    # Over the cap: older chats lose their snippets, then drop out, with a note.
    small = await sync_to_async(overview.overview)(ctx, 400)
    # The most recently active chat keeps its snippets; the older one is one line.
    assert "    reply: Hi from design main." in small
    assert "- Logo (thread" in small and "Make a logo" not in small
    tiny = await sync_to_async(overview.overview)(ctx, 200)
    assert "older chat(s) not shown" in tiny and "- Logo (thread" not in tiny

    # The block is in an orchestrating main chat's context, not seeded.
    engine._client.responses = [say("ok")]
    await boundcorp.ask(root, "What's going on?")
    system = _turn_context(engine._client.calls[-1])
    assert "## Bots and threads" in system
    assert "    reply: Hi from design main." in system
    assert "Resolving threads: keep the thread list" in system  # the built-in rule


@pytest.mark.django_db(transaction=True)
async def test_the_target_decides_whether_it_takes_new_threads(
    tmp_path, thread_messages, settings
):
    from django.conf import settings as django_settings

    stopped = []
    settings.DJANGO_ERGO = {
        **getattr(django_settings, "DJANGO_ERGO", {}),
        "THREAD_MESSAGE_RUNNER": "tests.test_bots.record_message",
        "TURN_STOPPER": lambda session_id: stopped.append(session_id) or True,
    }
    user = await User.objects.acreate(username="newthreads")
    engine = claude_engine()
    registry, boundcorp, design = _family(
        tmp_path,
        engine,
        "name: boundcorp\n",
        "name: design\norchestration: false\n",
    )
    root = await boundcorp.root_session(user)
    engine._client.responses = [
        claude_tool(
            "ergo_thread_send",
            {"bot": "design", "thread": "new", "message": "Logo"},
            tool_id="n1",
        ),
        say("ok"),
    ]
    await boundcorp.ask(root, "Design a logo")
    refused = _tool_results(engine)[-1]
    assert (
        refused["is_error"] and "design doesn't take new threads" in refused["content"]
    )

    design.definition.allow_create_sessions = True
    engine._client.responses = [
        claude_tool(
            "ergo_thread_send",
            {"bot": "design", "thread": "new", "title": "Logo", "message": "Logo"},
            tool_id="n2",
        ),
        say("ok"),
    ]
    await boundcorp.ask(root, "Design a logo")
    sent = json.loads(_tool_results(engine)[-1]["content"])
    thread = await ConversationSession.objects.aget(id=sent["thread_id"])
    assert thread.bot_name == "design"
    assert thread.metadata["started_by"] == str(root.id)
    assert thread.metadata["started_by_bot"] == "boundcorp"
    assert thread.metadata["started_by_label"] == "boundcorp · Main"

    # boundcorp manages the thread it started: stop it, then archive it.
    other = await design.create_session(user, title="Not ours")
    engine._client.responses = [
        claude_tool(
            "ergo_thread_stop",
            {"bot": "design", "thread_id": str(thread.id)},
            tool_id="x1",
        ),
        claude_tool(
            "ergo_thread_archive",
            {"bot": "design", "thread_id": str(other.id)},
            tool_id="x2",
        ),
        claude_tool(
            "ergo_thread_archive",
            {"bot": "design", "thread_id": str(thread.id)},
            tool_id="x3",
        ),
        say("Stopped and archived."),
    ]
    await boundcorp.ask(root, "Never mind the logo")
    stop, not_ours, archived = _tool_results(engine)[-3:]
    assert json.loads(stop["content"]) == {
        "thread": "design · Logo",
        "cancelled_messages": 1,  # the request was still queued
        "stopped_running_turn": True,
    }
    assert stopped == [str(thread.id)]
    assert not_ours["is_error"] and "only design can resolve it" in not_ours["content"]
    assert archived["content"].startswith("Resolved design · Logo")
    await thread.arefresh_from_db()
    assert thread.status == "completed"


@pytest.mark.django_db(transaction=True)
async def test_ready_to_resolve_is_marked_only_where_the_viewer_may_resolve(
    tmp_path, thread_messages
):
    from datetime import timedelta

    from django.utils import timezone

    from django_ergo.bots import orchestrator
    from django_ergo.bots import overview
    from django_ergo.bots.tools import ToolContext

    user = await User.objects.acreate(username="ownership")
    engine = claude_engine()
    registry, boundcorp, design = _family(
        tmp_path,
        engine,
        "name: boundcorp\n",
        "name: design\nthreads: {allow_create: true}\n",
    )
    root = await boundcorp.root_session(user)
    design_main = await design.root_session(user)
    own = await boundcorp.create_session(user, title="Own")
    started = await design.create_session(
        user,
        title="Started by boundcorp",
        metadata={
            "started_by": str(root.id),
            "started_by_bot": "boundcorp",
            "started_by_label": "boundcorp · Main",
        },
    )
    theirs = await design.create_session(
        user,
        title="Started by design",
        metadata={
            "started_by": str(design_main.id),
            "started_by_bot": "design",
            "started_by_label": "design · Main",
        },
    )
    threads = [own, started, theirs]
    quiet = timezone.now() - timedelta(hours=1)
    await ConversationSession.objects.filter(id__in=[t.id for t in threads]).aupdate(
        updated_at=quiet
    )

    ctx = ToolContext(bot=boundcorp, session=root, user=user)
    lines = (await sync_to_async(overview.overview)(ctx)).splitlines()

    def line_of(title):
        return next(line for line in lines if line.startswith(f"- {title} (thread"))

    assert "looks finished: ready to resolve" in line_of("Own")
    assert "looks finished: ready to resolve" in line_of("Started by boundcorp")
    # Another bot's own thread: shown as its owner's to close, not as a task.
    theirs_line = line_of("Started by design")
    assert "ready to resolve" not in theirs_line
    assert "looks finished (design resolves it)" in theirs_line

    # The block and the tool use one rule: marked iff can_resolve.
    for thread in threads:
        allowed = await sync_to_async(orchestrator.can_resolve)(
            boundcorp, user.id, thread
        )
        marked = "ready to resolve" in line_of(thread.metadata.get("title", ""))
        assert allowed is marked

    engine._client.responses = [
        claude_tool(
            "ergo_thread_resolve",
            {"bot": "design", "thread_id": str(theirs.id), "summary": "done"},
            tool_id="r1",
        ),
        claude_tool(
            "ergo_thread_resolve",
            {"bot": "design", "thread_id": str(started.id), "summary": "done"},
            tool_id="r2",
        ),
        claude_tool(
            "ergo_thread_resolve",
            {"thread_id": str(own.id), "summary": "done"},
            tool_id="r3",
        ),
        say("Tidied."),
    ]
    await boundcorp.ask(root, "Tidy up")
    refused, resolved_started, resolved_own = _tool_results(engine)[-3:]
    assert refused["is_error"]
    assert (
        "design · Started by design was started by design, not by this bot"
        in (refused["content"])
    )
    assert "only design can resolve it" in refused["content"]
    assert resolved_started["content"].startswith("Resolved design · Started by")
    assert resolved_own["content"].startswith("Resolved boundcorp · Own")
    await theirs.arefresh_from_db()
    assert theirs.status != "completed"

    # A thread started by a third bot names both bots that may resolve it.
    third = await design.create_session(
        user,
        title="Third",
        metadata={"started_by": str(design_main.id), "started_by_bot": "other"},
    )
    msg = orchestrator._refusal(third, "resolve")
    assert "design (its bot) or other (which started it)" in msg


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
    assert "Knowledge base: Kitchen" in _turn_context(first)
    assert "chocolate Soylent shake" in _turn_context(first)
    # What it knows is always in context; the kb tools load when needed.
    assert "ergo_kb_search" not in _tool_names(first)
    assert "- kb (not loaded):" in _turn_context(first)


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
    await ThreadMessage.objects.acreate(
        recipient_session=busy, text="still working", status="delivered"
    )
    ago = timezone.now() - timedelta(days=8)
    await ConversationSession.objects.filter(id__in=[root.id, old.id, busy.id]).aupdate(
        updated_at=ago
    )

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
    bot = Bot.load(
        write_bot(
            tmp_path,
            "name: tasker\ntools: [tools/pantry.py]\n",
            name="tasker",
            tools=TASK_TOOLS,
        )
    )
    assert ToolContext(bot=bot).tasks.run("slow_sum", [1, 2]) == "queued elsewhere"
    assert calls == [("tasker", "slow_sum", [[1, 2]], {})]
    assert background.execute("tasker", "slow_sum", [[1, 2]], {}) == 3


@pytest.mark.django_db(transaction=True)
async def test_messages_wait_while_the_recipient_waits_for_approval(
    tmp_path, thread_messages
):
    from django_ergo.bots import messaging
    from django_ergo.conversation.models import ThreadMessage

    user = await User.objects.acreate(username="patient")
    bot, engine = make_bot(
        tmp_path,
        claude_tool(
            "add_to_list", {"item": "milk"}, tool_id="add1"
        ),  # the thread pauses
        say("Added milk."),
        say("Noted."),  # the queued message, once the thread is free
    )
    root = await bot.root_session(user)
    thread = await bot.create_session(user, parent=root, title="List")
    await bot.ask(thread, "Add milk")
    assert await bot.pending_call(thread) is not None

    queued = await sync_to_async(messaging.send)(root, thread, "Also eggs?")
    await thread_messages()  # delivered while paused: stays queued
    await queued.arefresh_from_db()
    assert queued.status == "queued"

    # The approval is answered once, even if it's pressed twice.
    await bot.resume(thread, True)
    again = await bot.resume(thread, True)
    assert again.call is None
    assert [str(queued.id)] == SENT  # freed: the waiting message goes out
    await thread_messages()
    await queued.arefresh_from_db()
    assert queued.status == "answered"
    assert (
        await sync_to_async(messaging.deliver)(str(queued.id)) is None
    )  # a duplicate does nothing
    assert await ThreadMessage.objects.filter(in_reply_to=queued).acount() == 1


@pytest.mark.django_db(transaction=True)
def test_replies_count_toward_the_delegation_depth(tmp_path, thread_messages):
    from django_ergo.bots import messaging
    from django_ergo.conversation.models import StructuredCall
    from django_ergo.conversation.models import ThreadMessage

    user = User.objects.create(username="pingpong")
    a = ConversationSession.objects.create(
        user=user, bot_name="a", metadata={"bot_role": "root"}
    )
    b = ConversationSession.objects.create(
        user=user, bot_name="b", metadata={"bot_role": "root"}
    )
    reply = None
    for hop in range(1, messaging.MAX_DEPTH + 2):
        handling = reply
        StructuredCall.objects.filter(session=a).delete()
        if handling:
            StructuredCall.objects.create(
                kind="chat_reply",
                session=a,
                status="in_progress",
                metadata={"thread_message": str(handling.id)},
            )
        if hop > messaging.MAX_DEPTH:
            with pytest.raises(ValueError, match="Too many hops"):
                messaging.send(a, b, "again?")
            break
        request = messaging.send(a, b, "again?")
        assert request.depth == hop
        reply = ThreadMessage.objects.create(
            sender_session=b,
            recipient_session=a,
            in_reply_to=request,
            text="no",
            depth=request.depth,
        )


LAZY_YAML = """
    name: lazy
    tools: [tools/pantry.py]
    chats:
      main: {skills: []}
      reports:
        description: Weekly reports
        instructions: Keep reports short.
        skills: [pantry]
    threads: {skills: [pantry]}
    skills:
      unload_after_turns: 2
      requires: {planner: [pantry]}
"""


@pytest.mark.django_db(transaction=True)
async def test_skills_load_mid_turn_require_each_other_and_unload(tmp_path):
    user = await User.objects.acreate(username="lazy-user")
    bot, engine = make_bot(
        tmp_path,
        claude_tool("ergo_skill_load", {"name": "planner"}, tool_id="l1"),
        claude_tool("pantry_count", {"item": "eggs"}, tool_id="p1"),
        say("4 eggs."),
        yaml_text=LAZY_YAML,
        name="lazy",
    )
    skills = bot.definition.root_dir / "skills"
    skills.mkdir()
    (skills / "planner.md").write_text("# Plan meals\n\nCount the pantry first.")
    bot = Bot.load(bot.definition.root_dir, engine_factory=bot._engine_factory)

    main = await bot.main_session(user)
    assert main.metadata["bot_role"] == "main"
    await bot.ask(main, "Plan dinner")
    first, second, _ = engine._client.calls
    assert "pantry_count" not in _tool_names(first)
    assert "pantry_count" in _tool_names(
        second
    )  # loaded mid-turn, offered on the next call
    loaded = second["messages"][-1]["content"][0]["content"]
    assert "# Skill: planner" in loaded
    assert "Count the pantry first." in loaded
    assert "# Skill: pantry" in loaded  # required by planner
    await main.arefresh_from_db()
    assert set(main.metadata["skills"]) == {"planner", "pantry"}

    # Unused for more than unload_after_turns turns: dropped again.
    for _ in range(3):
        engine._client.responses = [say("ok")]
        await bot.ask(main, "hi")
    assert "pantry_count" not in _tool_names(engine._client.calls[-1])
    await main.arefresh_from_db()
    assert main.metadata["skills"] == {}

    # A named chat and a thread load the skills their config lists.
    reports = await bot.chat_session(user, "reports")
    assert (reports.metadata["bot_role"], reports.metadata["chat"]) == (
        "chat",
        "reports",
    )
    assert reports.metadata["title"] == "Weekly reports"
    assert await bot.chat_session(user, "reports") == reports
    engine._client.responses = [say("ok")]
    await bot.ask(reports, "hi")
    call = engine._client.calls[-1]
    assert "pantry_count" in _tool_names(call)
    assert call["system"].startswith("You run the kitchen.\n\nKeep reports short.")
    thread = await bot.create_session(user, parent=main)
    engine._client.responses = [say("ok")]
    await bot.ask(thread, "hi")
    assert "pantry_count" in _tool_names(engine._client.calls[-1])
    with pytest.raises(ValueError, match="no chat named"):
        await bot.chat_session(user, "nope")


@pytest.mark.django_db(transaction=True)
async def test_instructions_follow_agents_md(tmp_path):
    user = await User.objects.acreate(username="editor")
    bot, engine = make_bot(tmp_path, say("a"), say("b"))
    main = await bot.main_session(user)
    await bot.ask(main, "hi")
    (bot.definition.root_dir / "agents.md").write_text("You run a tidy kitchen.")
    bot = Bot.load(bot.definition.root_dir, engine_factory=bot._engine_factory)
    await bot.ask(main, "hi again")
    assert engine._client.calls[-1]["system"].startswith("You run a tidy kitchen.")


@pytest.mark.django_db(transaction=True)
async def test_thread_metadata_names_a_new_thread(tmp_path):
    from django_ergo.conversation.models import StructuredCall

    bot, _ = make_bot(
        tmp_path, claude_tool("submit_output", {"title": '"Fix the pantry sync."'})
    )
    user = await get_user_model().objects.acreate(username="namer")
    assert await bot.thread_metadata(
        "the pantry sync keeps failing, can you look?", user=user
    ) == {"title": "Fix the pantry sync"}
    call = await StructuredCall.objects.aget(kind="new_thread_metadata")
    assert call.status == "completed"


@pytest.mark.django_db
def test_max_turns_comes_from_bot_yaml(tmp_path):
    bot, _ = make_bot(tmp_path)
    assert bot.reply_spec([]).max_turns == 50
    bot, _ = make_bot(
        tmp_path / "x",
        yaml_text="name: kitchen\nmax_turns: 80\ntools: [tools/pantry.py]\n",
    )
    assert bot.reply_spec([]).max_turns == 80


@pytest.mark.django_db(transaction=True)
async def test_files_shared_with_a_thread_message_are_listed_for_the_recipient(
    tmp_path, thread_messages
):
    from asgiref.sync import sync_to_async

    from django_ergo.bots.messaging import turn_text
    from django_ergo.conversation.attachments import save_session_file
    from django_ergo.conversation.models import ThreadMessage

    user = await User.objects.acreate(username="sharer")
    bot, engine = make_bot(
        tmp_path,
        claude_tool(
            "ergo_thread_send",
            {"thread": "new", "message": "Build this", "attachments": ["mock.png"]},
        ),
        claude_tool(
            "ergo_thread_send",
            {"thread": "new", "message": "x", "attachments": ["nope.png"]},
            tool_id="s2",
        ),
        say("Sent."),
    )
    root = await bot.root_session(user)
    png = await sync_to_async(save_session_file)(root, "mock.png", b"\x89PNG fake")
    await bot.ask(root, "Hand off the mockup")

    request = await ThreadMessage.objects.select_related(
        "sender_session", "recipient_session"
    ).aget()
    assert request.metadata["attachments"] == [
        {
            "id": str(png.id),
            "filename": "mock.png",
            "media_type": "image/png",
            "size": png.size,
            "session": str(root.id),
        }
    ]
    text = await sync_to_async(turn_text)(request)
    assert f"- mock.png (image/png, {png.size:,} bytes), id {png.id}" in text
    # An unknown file fails the send, naming what the chat has.
    failed = engine._client.calls[2]["messages"][-1]["content"][0]
    assert (
        failed["is_error"]
        and "No file 'nope.png' in that chat (it has: mock.png)" in failed["content"]
    )


PING_TOOLS = (
    TOOLS
    + '''
@bot_tool(takes_context=True)
def parent_sends(ctx, message: str, interrupt: bool = False) -> dict:
    """Another chat's send to this thread, arriving while this turn runs."""
    from django_ergo.bots import messaging, orchestrator
    from django_ergo.bots.tools import ToolContext
    from django_ergo.conversation.models import ThreadMessage

    parent = ToolContext(bot=ctx.bot, session=ctx.session.parent, user=ctx.user)
    sent = orchestrator.ergo_thread_send(
        parent, message=message, thread=str(ctx.session.id), interrupt=interrupt
    )
    messaging.deliver(sent["message_id"])  # a worker picks it up mid-turn
    sent["status_now"] = ThreadMessage.objects.get(id=sent["message_id"]).status
    return sent
'''
)


@pytest.fixture
def turn_stopper(settings, thread_messages):
    """A TURN_STOPPER that records which sessions it was asked to stop."""
    stopped = []
    settings.DJANGO_ERGO = {
        **settings.DJANGO_ERGO,
        "TURN_STOPPER": lambda session_id: stopped.append(session_id) or True,
    }
    return stopped


def _heard(call):
    """The thread messages (and the user's "Hello") the model was shown in a call."""
    return [
        text
        for text in _texts(call)
        if text == "Hello"
        or text.startswith(("[Forwarded by", "[Message from", "[Report from"))
    ]


@pytest.mark.django_db(transaction=True)
async def test_a_send_right_after_a_forward_to_the_same_thread_is_queued_in_order(
    tmp_path, thread_messages
):
    from django_ergo.bots import messaging
    from django_ergo.conversation.models import ThreadMessage

    user = await User.objects.acreate(username="forwarder")
    bot, engine = make_bot(tmp_path, say("ok"))
    root = await bot.root_session(user)
    thread = await bot.create_session(user, parent=root, title="Deploy")
    engine._client.responses = [
        claude_tool("ergo_thread_forward", {"thread": str(thread.id)}, tool_id="fwd"),
        claude_tool(
            "ergo_thread_send",
            {"thread": str(thread.id), "message": "It is the octo cluster."},
            tool_id="ctx",
        ),
        say("Sent to kitchen · Deploy."),
    ]
    await bot.ask(root, "Merge the octo fix")
    forwarded, follow_up = _tool_results(engine)[-2:]
    assert not forwarded.get("is_error")
    assert not follow_up.get("is_error"), follow_up["content"]
    queued = json.loads(follow_up["content"])
    assert (queued["status"], queued["queue_position"]) == ("queued", 2)
    first, second = [m async for m in ThreadMessage.objects.order_by("created_at")]
    assert first.metadata["forwarded"] and second.text == "It is the octo cluster."
    assert len(SENT) == 2

    # Whichever delivery a worker runs first, the thread sees the forward first.
    engine._client.responses = [say("Merging."), say("Noted: octo.")]
    await sync_to_async(messaging.deliver)(str(second.id))
    await thread_messages()
    await thread_messages()
    seen_first, seen_last = (_heard(call) for call in engine._client.calls[-2:])
    assert len(seen_first) == 1 and seen_first[0].startswith("[Forwarded by")
    assert len(seen_last) == 2 and seen_last[1].endswith("It is the octo cluster.")
    statuses = [m.status async for m in ThreadMessage.objects.order_by("created_at")]
    assert statuses[:2] == ["answered", "answered"]
    assert len(engine._client.calls) == 5  # the root's 3 model calls and 2 thread turns


@pytest.mark.django_db(transaction=True)
async def test_two_reports_in_a_row_are_not_nudges(tmp_path, thread_messages):
    from django_ergo.conversation.models import ThreadMessage

    user = await User.objects.acreate(username="reporter2")
    bot, engine = make_bot(tmp_path, say("ok"))
    root = await bot.root_session(user)
    thread = await bot.create_session(user, parent=root, title="Status")
    engine._client.responses = [
        claude_tool(
            "ergo_thread_send",
            {"thread": "main", "message": "Halfway there"},
            tool_id="r1",
        ),
        claude_tool(
            "ergo_thread_send",
            {"thread": "main", "message": "Done: PR #7"},
            tool_id="r2",
        ),
        say("Reported twice."),
    ]
    await bot.ask(thread, "Report as you go")  # a thread's messages to main are reports
    first, second = _tool_results(engine)[-2:]
    assert not first.get("is_error") and not second.get("is_error"), second["content"]
    assert await ThreadMessage.objects.filter(metadata__report=True).acount() == 2


@pytest.mark.django_db(transaction=True)
async def test_follow_ups_to_an_idle_thread_are_all_accepted_and_each_reply_comes_back(
    tmp_path, thread_messages
):
    from django_ergo.bots import orchestrator
    from django_ergo.conversation.models import ThreadMessage

    user = await User.objects.acreate(username="followuper")
    bot, engine = make_bot(tmp_path, say("ok"))
    root = await bot.root_session(user)
    thread = await bot.create_session(user, parent=root, title="Mockups")
    send = lambda text, tool_id: claude_tool(  # noqa: E731
        "ergo_thread_send", {"thread": str(thread.id), "message": text}, tool_id=tool_id
    )
    engine._client.responses = [
        send("Draw the header", "s1"),
        send("Make it blue", "s2"),
        send("And add a logo", "s3"),
        say("Asked three times."),
    ]
    await bot.ask(root, "Get the mockups drawn")
    sent = [json.loads(r["content"]) for r in _tool_results(engine)[-3:]]
    assert [(s["status"], s.get("queue_position")) for s in sent] == [
        ("sent", None),
        ("queued", 2),
        ("queued", 3),
    ]
    assert not any(r.get("is_error") for r in _tool_results(engine)[-3:])
    status = await sync_to_async(orchestrator.thread_status)(root)
    assert status["waiting_on"] == 3
    assert (await sync_to_async(orchestrator.thread_status)(thread))["working_for"] == 3

    # The thread takes them one turn each, oldest first, and each is answered.
    engine._client.responses = [
        say("Header drawn."),
        say("Blue now."),
        say("Logo added."),
        say("Noted: header."),  # then the root takes each reply as a turn
        say("Noted: blue."),
        say("Noted: logo."),
    ]
    while SENT:
        await thread_messages()
    rows = [m async for m in ThreadMessage.objects.order_by("created_at")]
    requests = [m for m in rows if m.in_reply_to_id is None]
    assert [(m.text, m.status, m.reply_text) for m in requests] == [
        ("Draw the header", "answered", "Header drawn."),
        ("Make it blue", "answered", "Blue now."),
        ("And add a logo", "answered", "Logo added."),
    ]
    replies = [m for m in rows if m.in_reply_to_id is not None]
    assert sorted(m.in_reply_to_id for m in replies) == sorted(m.id for m in requests)
    assert all(m.status == "answered" for m in replies)  # each reached the root's turn
    asked = [
        m
        for call in engine._client.calls
        for m in _heard(call)
        if m.startswith("[Message from")
    ]
    assert [text.rsplit("\n\n", 1)[-1] for text in dict.fromkeys(asked)] == [
        "Draw the header",
        "Make it blue",
        "And add a logo",
    ]
    assert (await sync_to_async(orchestrator.thread_status)(root))["waiting_on"] == 0
    assert (await sync_to_async(orchestrator.thread_status)(thread))["working_for"] == 0

    # Once idle again, another follow-up is simply delivered.
    engine._client.responses = [send("One more thing", "s4"), say("Asked again.")]
    await bot.ask(root, "And one more")
    again = json.loads(_tool_results(engine)[-1]["content"])
    assert again["status"] == "sent" and "queue_position" not in again


@pytest.mark.django_db(transaction=True)
async def test_follow_ups_to_a_busy_thread_queue_in_order_and_each_reply_closes_its_request(
    tmp_path, thread_messages
):
    from django_ergo.bots import orchestrator
    from django_ergo.conversation.models import ThreadMessage

    user = await User.objects.acreate(username="busyfollower")
    bot, engine = make_bot(tmp_path, say("ok"), tools=PING_TOOLS)
    root = await bot.root_session(user)
    thread = await bot.create_session(user, parent=root, title="Deploy")
    engine._client.responses = [
        claude_tool(
            "ergo_thread_send",
            {"thread": str(thread.id), "message": "Deploy staging"},
            tool_id="a",
        ),
        say("Asked."),
    ]
    await bot.ask(root, "Ship it")
    first = await ThreadMessage.objects.aget(in_reply_to__isnull=True)

    # While the thread works on that request (still open), root follows up twice.
    engine._client.responses = [
        claude_tool("parent_sends", {"message": "Use the blue cluster"}, tool_id="b"),
        claude_tool(
            "parent_sends", {"message": "Then run the smoke test"}, tool_id="c"
        ),
        say("Staging deployed."),
    ]
    await thread_messages()  # the thread starts on it
    queued = [json.loads(r["content"]) for r in _tool_results(engine)[-2:]]
    assert [(q["status"], q["queue_position"], q["status_now"]) for q in queued] == [
        ("queued", 1, "queued"),
        ("queued", 2, "queued"),
    ]
    await first.arefresh_from_db()
    assert (first.status, first.reply_text) == ("answered", "Staging deployed.")
    status = await sync_to_async(orchestrator.thread_status)(root)
    assert status["waiting_on"] == 2  # the two follow-ups are still open

    # The turn's end sends them on: one turn each, in the order they were sent.
    engine._client.responses = [
        say("On the blue cluster."),
        say("Smoke test passed."),
        say("Noted: staging."),  # then the root takes each reply as a turn
        say("Noted: blue."),
        say("Noted: smoke."),
    ]
    while SENT:
        await thread_messages()
    rows = [m async for m in ThreadMessage.objects.order_by("created_at")]
    requests = [m for m in rows if m.in_reply_to_id is None]
    assert [(m.text, m.status, m.reply_text) for m in requests] == [
        ("Deploy staging", "answered", "Staging deployed."),
        ("Use the blue cluster", "answered", "On the blue cluster."),
        ("Then run the smoke test", "answered", "Smoke test passed."),
    ]
    replies = [m for m in rows if m.in_reply_to_id is not None]
    assert sorted(m.in_reply_to_id for m in replies) == sorted(m.id for m in requests)
    assert all(m.status == "answered" for m in replies)
    assert (await sync_to_async(orchestrator.thread_status)(root))["waiting_on"] == 0
    assert (await sync_to_async(orchestrator.thread_status)(thread))["working_for"] == 0


@pytest.mark.django_db(transaction=True)
async def test_a_send_to_a_thread_busy_with_the_users_turn_queues_without_interrupting(
    tmp_path, turn_stopper, thread_messages
):
    from django_ergo.conversation.models import ThreadMessage

    user = await User.objects.acreate(username="humanbusy")
    bot, engine = make_bot(tmp_path, say("ok"), tools=PING_TOOLS)
    root = await bot.root_session(user)
    thread = await bot.create_session(user, parent=root, title="Plan")
    engine._client.responses = [
        claude_tool(
            "parent_sends",
            {"message": "Also check the eggs", "interrupt": True},
            tool_id="p1",
        ),
        say("Hello, Lee."),  # the user's turn, to its end
        say("Eggs checked."),  # the queued message's turn
    ]
    result = await bot.ask(thread, "Hello")
    assert result.text == "Hello, Lee."  # the user's request was never cancelled
    sent = json.loads(_tool_results(engine)[-1]["content"])
    assert (sent["status"], sent["queue_position"]) == ("queued", 1)
    assert sent["interrupted"] is False and "never the user's" in sent["note"]
    assert sent["status_now"] == "queued"  # a worker found the thread busy: left alone
    assert turn_stopper == []
    assert await thread.structured_calls.filter(status="completed").acount() == 1

    # The turn's end sends it on, as the next turn.
    await thread_messages()
    message = await ThreadMessage.objects.aget(id=sent["message_id"])
    assert (message.status, message.reply_text) == ("answered", "Eggs checked.")
    heard = _heard(engine._client.calls[-1])
    assert heard[0] == "Hello" and heard[1].endswith("Also check the eggs")


@pytest.mark.django_db(transaction=True)
async def test_interrupt_only_stops_a_turn_answering_this_chats_own_request(
    tmp_path, turn_stopper, thread_messages, settings
):
    from django_ergo.bots import orchestrator
    from django_ergo.bots.tools import ToolContext
    from django_ergo.conversation.models import StructuredCall
    from django_ergo.conversation.models import ThreadMessage

    user = await User.objects.acreate(username="interrupter")
    bot, _ = make_bot(tmp_path, say("ok"))
    root = await bot.root_session(user)
    other = await bot.create_session(user, parent=root, title="Other")
    thread = await bot.create_session(user, parent=root, title="Work")
    ctx = ToolContext(bot=bot, session=root, user=user)

    async def interrupt(running_for=None, *, idle=False):
        """Root sends "Never mind" with interrupt while the thread is idle, or runs a
        turn answering ``running_for`` (kwargs of a ThreadMessage; None: the user)."""
        await StructuredCall.objects.all().adelete()
        await ThreadMessage.objects.all().adelete()
        turn = None
        if running_for is not None:
            turn = await ThreadMessage.objects.acreate(
                recipient_session=thread, status="delivered", **running_for
            )
        if not idle:
            await StructuredCall.objects.acreate(
                kind="chat_reply",
                session=thread,
                user=user,
                request="x",
                metadata={"thread_message": str(turn.id)} if turn else {},
            )
        turn_stopper.clear()
        return await sync_to_async(orchestrator.ergo_thread_send)(
            ctx, message="Never mind", thread=str(thread.id), interrupt=True
        )

    # Idle: nothing to interrupt, the message just goes.
    idle = await interrupt(idle=True)
    assert (idle["status"], idle["interrupted"]) == ("sent", False)
    assert "nothing is running there" in idle["note"] and turn_stopper == []

    # The user's own turn, a message of theirs that this chat forwarded, and another
    # chat's request are never stopped; the message queues behind them.
    users_turn = await interrupt()
    forwarded = await interrupt(
        {
            "sender_session": root,
            "text": "Yes, merge it",
            "metadata": {"forwarded": {"author": "Lee"}, "report": True},
        }
    )
    others = await interrupt({"sender_session": other, "text": "Plan Monday"})
    for refused, why in (
        (users_turn, "never the user's"),
        (forwarded, "a bot never cancels the user's request"),
        (others, "only your own request can be interrupted"),
    ):
        assert (refused["status"], refused["interrupted"]) == ("queued", False)
        assert why in refused["note"]
    assert turn_stopper == []

    # Its own running request is replaced: the turn is stopped, this goes out next.
    own = await interrupt({"sender_session": root, "text": "Draw it"})
    assert (own["status"], own["interrupted"]) == ("queued", True)
    assert turn_stopper == [str(thread.id)]
    assert "Its running turn was stopped" in own["note"]

    # Without a TURN_STOPPER nothing can be stopped: the message still goes out, queued
    # behind the request that is still open, and says why the interrupt didn't apply.
    settings.DJANGO_ERGO = {**settings.DJANGO_ERGO, "TURN_STOPPER": None}
    unstoppable = await interrupt({"sender_session": root, "text": "Draw it"})
    assert (unstoppable["status"], unstoppable["interrupted"]) == ("queued", False)
    assert "can't stop a running turn" in unstoppable["note"]


@pytest.mark.django_db(transaction=True)
async def test_queued_messages_go_out_after_a_turn_that_fails(
    tmp_path, thread_messages, monkeypatch
):
    from django_ergo.bots import messaging

    user = await User.objects.acreate(username="crasher")
    bot, engine = make_bot(tmp_path, say("Second done."))
    root = await bot.root_session(user)
    thread = await bot.create_session(user, parent=root, title="Crashy")
    first = await sync_to_async(messaging.send)(root, thread, "First")
    second = await sync_to_async(messaging.send)(root, thread, "Second")

    real_ask = bot.ask
    asked = []

    async def flaky(session, message, **kwargs):
        asked.append(message)
        if len(asked) == 1:
            msg = "the worker crashed"
            raise RuntimeError(msg)
        return await real_ask(session, message, **kwargs)

    monkeypatch.setattr(bot, "ask", flaky)
    SENT.clear()
    await sync_to_async(messaging.deliver)(str(second.id))  # serves the oldest
    await first.arefresh_from_db()
    assert (first.status, first.error) == ("failed", "the worker crashed")
    assert str(second.id) in SENT  # the failure sent the queue on

    await sync_to_async(messaging.deliver)(str(second.id))
    await second.arefresh_from_db()
    assert (second.status, second.reply_text) == ("answered", "Second done.")
    assert asked[0].endswith("First") and asked[1].endswith("Second")
    assert await thread.thread_messages.filter(status="queued").acount() == 0


@pytest.mark.django_db(transaction=True)
async def test_queued_messages_go_out_after_a_stopped_turn(
    tmp_path, thread_messages, settings
):
    from django_ergo.bots import messaging
    from django_ergo.conversation.structured import TurnSignal

    class StopFirst:
        def __init__(self):
            self.checks = 0

        async def check(self):
            self.checks += 1
            return TurnSignal(stop=True)

    controls = [StopFirst()]  # only the first delivered turn is stopped
    settings.DJANGO_ERGO = {
        **settings.DJANGO_ERGO,
        "TURN_CONTROL": lambda session, delegated: controls.pop() if controls else None,
    }
    user = await User.objects.acreate(username="stopper")
    bot, engine = make_bot(tmp_path, say("Second done."))
    root = await bot.root_session(user)
    thread = await bot.create_session(user, parent=root, title="Stopped")
    first = await sync_to_async(messaging.send)(root, thread, "First")
    second = await sync_to_async(messaging.send)(root, thread, "Second")

    SENT.clear()
    await sync_to_async(messaging.deliver)(str(first.id))
    await first.arefresh_from_db()
    assert first.status == "answered" and first.reply_text.startswith("(no reply:")
    assert len(engine._client.calls) == 0  # stopped before the model was called
    assert str(second.id) in SENT  # the stop sent the queue on

    await sync_to_async(messaging.deliver)(str(second.id))
    await second.arefresh_from_db()
    assert (second.status, second.reply_text) == ("answered", "Second done.")


@pytest.mark.django_db(transaction=True)
async def test_stopping_a_thread_cancels_everything_this_bot_queued_for_it(
    tmp_path, turn_stopper, thread_messages
):
    from django_ergo.conversation.models import ThreadMessage

    user = await User.objects.acreate(username="canceller")
    bot, engine = make_bot(tmp_path, say("ok"))
    root = await bot.root_session(user)
    thread = await bot.create_session(user, parent=root, title="Deploy")
    engine._client.responses = [
        claude_tool("ergo_thread_forward", {"thread": str(thread.id)}, tool_id="fwd"),
        claude_tool(
            "ergo_thread_send",
            {"thread": str(thread.id), "message": "Use the octo cluster"},
            tool_id="ctx",
        ),
        say("Sent."),
        claude_tool("ergo_thread_stop", {"thread_id": str(thread.id)}, tool_id="stop"),
        say("Stopped."),
    ]
    await bot.ask(root, "Deploy octo")
    assert [m.status async for m in ThreadMessage.objects.all()] == ["queued"] * 2
    await bot.ask(root, "Never mind")
    stopped = json.loads(_tool_results(engine)[-1]["content"])
    assert stopped["cancelled_messages"] == 2
    assert turn_stopper == [str(thread.id)]

    calls = len(engine._client.calls)
    for _ in range(len(SENT)):
        await thread_messages()  # the dispatches that were already on their way
    assert len(engine._client.calls) == calls  # nothing ran in the thread
    rows = [m async for m in ThreadMessage.objects.order_by("created_at")]
    assert [(m.status, m.error) for m in rows] == [
        ("failed", "cancelled by the sender")
    ] * 2


@pytest.mark.django_db(transaction=True)
def test_concurrent_sends_and_deliveries_lose_nothing_and_keep_order(
    tmp_path, thread_messages
):
    import threading

    from asgiref.sync import async_to_sync
    from django.db import connections

    from django_ergo.bots import messaging
    from django_ergo.conversation.models import ThreadMessage

    count = 6
    user = User.objects.create(username="rush")
    bot, engine = make_bot(tmp_path, *[say(f"done {i}") for i in range(count)])
    root = async_to_sync(bot.root_session)(user)
    thread = async_to_sync(bot.create_session)(user, parent=root, title="Rush")
    errors = []

    def together(work, arguments):
        """Run ``work(argument)`` for every argument at once, on threads of their own."""
        start = threading.Barrier(len(arguments))

        def run(argument):
            try:
                start.wait()
                work(argument)
            except Exception as exc:  # noqa: BLE001 - reported below
                errors.append(exc)
            finally:
                connections.close_all()

        threads = [threading.Thread(target=run, args=(a,)) for a in arguments]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

    together(lambda i: messaging.send(root, thread, f"task {i}"), range(count))
    queue = ThreadMessage.objects.filter(recipient_session=thread)
    texts = list(queue.order_by("created_at").values_list("text", flat=True))
    assert sorted(texts) == sorted(f"task {i}" for i in range(count))  # none lost

    ids = [str(i) for i in queue.order_by("created_at").values_list("id", flat=True)]
    together(messaging.deliver, ids)  # every worker at once
    for _ in range(count):  # what the workers left queued goes out in turn
        waiting = queue.filter(status="queued").order_by("created_at").first()
        if waiting is None:
            break
        messaging.deliver(str(waiting.id))
    assert errors == []

    answered = queue.order_by("created_at")
    assert [m.status for m in answered] == ["answered"] * count
    # Each ran exactly once, in the order sent: the nth turn gave the nth answer.
    assert [m.reply_text for m in answered] == [f"done {i}" for i in range(count)]
    assert len(engine._client.calls) == count


def test_open_prs_come_from_the_open_prs_setting(tmp_path, settings):
    from django.conf import settings as django_settings

    from django_ergo.bots import overview

    bot = Bot.load(write_bot(tmp_path, "name: lister\npull_requests: [a/b]\n"))
    settings.DJANGO_ERGO = {
        **getattr(django_settings, "DJANGO_ERGO", {}),
        "OPEN_PRS": lambda bot: [
            {"repo": "a/b", "number": 7, "title": "Fix it", "draft": True},
            {"repo": "a/b", "number": 8, "title": "Ship it"},
        ],
    }
    assert overview.open_prs(bot) == ["- a/b #7 (draft): Fix it", "- a/b #8: Ship it"]


def test_introspection_reads_the_bot_folder_and_ergo_but_nothing_hidden(tmp_path):
    from django_ergo.bots.introspection import introspection_toolkit
    from django_ergo.bots.tools import ToolContext

    bot, _ = make_bot(tmp_path)
    folder = bot.definition.root_dir
    (folder / ".env").write_text("SECRET=1")
    (folder / "tools" / "big.py").write_text(
        "\n".join(f"x{i} = {i}" for i in range(1000))
    )
    skill = next(s for s in bot.skill_defs if s.name == "introspection")
    assert skill.source == "built-in"
    tools = {
        name: tool.function
        for name, tool in introspection_toolkit(bot, ToolContext(bot=bot)).tools.items()
    }
    assert set(tools) == {"ergo_self_overview", "ergo_self_files", "ergo_self_read"}

    overview = tools["ergo_self_overview"]()
    assert overview["name"] == "kitchen"
    assert {"name": "pantry", "source": "tools/pantry.py"}.items() <= next(
        s for s in overview["skills"] if s["name"] == "pantry"
    ).items()
    listing = tools["ergo_self_files"]()
    assert "bot.yaml" in listing and "tools/pantry.py" in listing
    assert ".env" not in listing
    assert "kitchen" in tools["ergo_self_read"]("bot.yaml")
    piece = tools["ergo_self_read"]("tools/big.py", start_line=401)
    assert piece.startswith(
        "tools/big.py lines 401-800 of 1000 (read on with start_line=801)"
    )
    assert "def introspection_toolkit" in tools["ergo_self_read"](
        "ergo:bots/introspection.py"
    )
    assert "plugins/attachments.py" in tools["ergo_self_files"]("ergo:plugins")
    for bad in (".env", "../other", "/etc/passwd", "ergo:../../x"):
        with pytest.raises(ValueError, match="outside|hidden|exist"):
            tools["ergo_self_read"](bad)
    # A path that starts with the folder's own name gets a suggestion.
    with pytest.raises(ValueError, match=r"relative to your bot folder") as err:
        tools["ergo_self_read"](f"{folder.name}/tools/big.py")
    assert "did you mean 'tools/big.py'?" in str(err.value)


@pytest.mark.django_db(transaction=True)
async def test_a_report_upward_gets_no_reply_unless_it_asks(tmp_path, thread_messages):
    from django_ergo.conversation.models import ThreadMessage

    user = await User.objects.acreate(username="reporter")
    bot, engine = make_bot(tmp_path, say("ok"))
    root = await bot.root_session(user)
    thread = await bot.create_session(
        user,
        parent=root,
        title="Deploy",
        metadata={"started_by": str(root.id), "started_by_label": "kitchen · Main"},
    )
    engine._client.responses = [
        claude_tool(
            "ergo_thread_send", {"thread": "main", "message": "Deployed."}, tool_id="r1"
        ),
        say("Reported."),
    ]
    await bot.ask(thread, "Ship it")
    # The thread knows it's the thread, and who started it.
    system = _turn_context(engine._client.calls[0])
    assert (
        "## This chat\nYou are kitchen · Deploy, a thread started by kitchen · Main."
        in system
    )
    sent = json.loads(_tool_results(engine)[-1]["content"])
    assert sent["note"].startswith("Sent as a report")

    engine._client.responses = [say("Noted: deployed.")]
    await thread_messages()  # main reads the report...
    report = await ThreadMessage.objects.aget(recipient_session=root)
    assert report.metadata["report"] is True and report.status == "answered"
    assert engine._client.calls[-1]["messages"][0]["content"][1]["text"].startswith(
        "[Report from kitchen · Deploy"
    )
    assert SENT == []  # ...and no acknowledgement starts a turn in the thread

    # Asking gets an answer back.
    engine._client.responses = [
        claude_tool(
            "ergo_thread_send",
            {"thread": "main", "message": "Which cluster?", "ask": True},
            tool_id="r2",
        ),
        say("Asked."),
        say("The octo cluster."),
    ]
    await bot.ask(thread, "Next step")
    await thread_messages()
    assert len(SENT) == 1  # the answer is on its way back to the thread
    main_system = _turn_context(engine._client.calls[-1])
    assert "## This chat\nYou are kitchen · Main, the main chat." in main_system


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    "author",
    [None, {"kind": "telegram_user", "ref": "67890", "display_name": "Sam External"}],
)
async def test_forward_hands_the_users_own_message_to_a_thread(
    tmp_path, thread_messages, author
):
    from django_ergo.conversation.attachments import Attachment
    from django_ergo.conversation.identity import django_user_identity
    from django_ergo.conversation.models import ThreadMessage

    user = await User.objects.acreate(username="forwarder", first_name="Lee")
    bot, engine = make_bot(tmp_path, say("ok"))
    root = await bot.root_session(user)
    thread = await bot.create_session(user, parent=root, title="Deploy")
    engine._client.responses = [
        claude_tool(
            "ergo_thread_forward",
            {"thread": str(thread.id), "note": "Deploy owns octo."},
            tool_id="fwd",
        ),
        say("Sent to kitchen · Deploy."),
    ]
    words = "Yes, go ahead and merge the octo fix."
    await bot.ask(
        root,
        words,
        attachments=[Attachment("text/plain", data=b"log", filename="log.txt")],
        author=author,
    )
    sent = json.loads(_tool_results(engine)[-1]["content"])
    assert sent["forwarded_to"] == "kitchen · Deploy"
    assert sent["shared_files"] == ["log.txt"]

    forwarded = await ThreadMessage.objects.aget(recipient_session=thread)
    assert forwarded.text == words  # verbatim, not retold
    meta = forwarded.metadata
    expected_author = author or django_user_identity(user)
    assert meta["message_author"] == expected_author and meta["report"] is True
    assert meta["message_provenance"]["note"] == "Deploy owns octo."
    original = await root.messages.aget(sequence=0)
    origin = meta["message_provenance"]["origin"]
    assert original.author == expected_author
    assert origin["message_id"] == str(original.id)
    assert origin["sequence"] == original.sequence
    assert origin["timestamp"] == original.created_at.isoformat()
    assert origin["session_id"] == str(root.id)
    assert meta["message_provenance"]["forwarded_by"]["kind"] == "bot"

    engine._client.responses = [say("Merging now.")]
    await thread_messages()
    text = engine._client.calls[-1]["messages"][0]["content"][1]["text"]
    assert text.startswith("[Forwarded by kitchen · Main")
    assert words in text and "[Note from kitchen · Main: Deploy owns octo.]" in text
    assert "log.txt" in text
    assert expected_author["display_name"] in text
    incoming = await thread.messages.aget(sequence=0)
    assert incoming.author == expected_author
    assert incoming.provenance == meta["message_provenance"]
    assert await incoming.content_blocks.values_list("text", flat=True).aget() == words
    assert SENT == []  # the thread answers the user there; nothing comes back

    # A turn that answers another chat has no user message to forward.
    from django_ergo.bots import orchestrator

    assert await sync_to_async(orchestrator.user_message)(root) is None


@pytest.mark.django_db(transaction=True)
async def test_forwarding_again_preserves_original_external_author_and_origin(
    tmp_path, thread_messages
):
    from django_ergo.conversation.models import ThreadMessage

    user = await User.objects.acreate(username="relay-owner")
    author = {"kind": "telegram_user", "ref": "456", "display_name": "Outside Author"}
    bot, engine = make_bot(tmp_path, say("ok"))
    root = await bot.root_session(user)
    first = await bot.create_session(user, parent=root, title="First")
    second = await bot.create_session(user, parent=root, title="Second")
    engine._client.responses = [
        claude_tool(
            "ergo_thread_forward", {"thread": str(first.id)}, tool_id="forward-1"
        ),
        say("Sent to First."),
        claude_tool(
            "ergo_thread_forward", {"thread": str(second.id)}, tool_id="forward-2"
        ),
        say("Sent to Second."),
        say("Done."),
    ]
    words = "  Original request.\nKeep the whitespace.  "
    await bot.ask(root, words, author=author)
    source = await root.messages.aget(sequence=0)
    await thread_messages()
    relayed = await ThreadMessage.objects.aget(recipient_session=second)
    provenance = relayed.metadata["message_provenance"]
    assert relayed.text == words
    assert relayed.metadata["message_author"] == author
    assert provenance["origin"]["session_id"] == str(root.id)
    assert provenance["origin"]["message_id"] == str(source.id)
    assert provenance["origin"]["timestamp"] == source.created_at.isoformat()
    assert provenance["forwarded_by"]["session_id"] == str(first.id)
    await thread_messages()
    incoming = await second.messages.aget(sequence=0)
    assert incoming.author == author and incoming.provenance == provenance
    assert await incoming.content_blocks.values_list("text", flat=True).aget() == words
    assert SENT == []


@pytest.mark.django_db(transaction=True)
async def test_queued_legacy_forward_delivers_raw_text_and_structured_identity(
    tmp_path, thread_messages
):
    from django_ergo.conversation.models import ThreadMessage

    user = await User.objects.acreate(username="legacy-owner")
    bot, engine = make_bot(tmp_path, say("Handled."))
    root = await bot.root_session(user)
    thread = await bot.create_session(user, parent=root, title="Legacy")
    message = await ThreadMessage.objects.acreate(
        sender_session=root,
        recipient_session=thread,
        text="Unprefixed original.",
        metadata={
            "forwarded": {
                "author": "Original User",
                "user_id": 987,
                "sent_at": "2026-10-05T11:00:00+00:00",
                "source_call": "old-call",
            },
            "report": True,
        },
    )
    SENT.append(str(message.id))
    await thread_messages()
    incoming = await thread.messages.aget(sequence=0)
    assert incoming.author == {
        "kind": "django_user",
        "ref": "987",
        "display_name": "Original User",
    }
    assert incoming.provenance["kind"] == "forwarded"
    assert incoming.provenance["origin"]["source_call"] == "old-call"
    assert (
        await incoming.content_blocks.values_list("text", flat=True).aget()
        == message.text
    )
    assert engine._client.calls[0]["messages"][0]["content"][1]["text"].startswith(
        "[Forwarded by kitchen · Main"
    )
    assert SENT == []


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    ("metadata", "display_name"),
    [
        ({"worker": "worker-1"}, "Worker"),
        ({"schedule": "weekly-plan"}, "Scheduled message"),
    ],
)
async def test_senderless_system_thread_messages_are_not_human_authored(
    tmp_path, thread_messages, metadata, display_name
):
    from django_ergo.bots import messaging
    from django_ergo.conversation.models import ThreadMessage

    user = await User.objects.acreate(username="system-owner")
    bot, engine = make_bot(tmp_path, say("Noted."))
    root = await bot.root_session(user)
    message = await ThreadMessage.objects.acreate(
        recipient_session=root, text="System result.", metadata=metadata
    )
    expected = await sync_to_async(messaging.turn_text)(message)
    SENT.append(str(message.id))
    await thread_messages()
    incoming = await root.messages.aget(sequence=0)
    assert incoming.author["kind"] == "system"
    assert incoming.author["display_name"] == display_name
    assert engine._client.calls[0]["messages"][0]["content"][1]["text"] == expected


@pytest.mark.django_db(transaction=True)
async def test_finished_threads_are_resolved_and_unfinished_ones_refused(
    tmp_path, thread_messages
):
    from django_ergo.bots import orchestrator
    from django_ergo.conversation.models import Worker

    user = await User.objects.acreate(username="resolver")
    bot, engine = make_bot(tmp_path, say("ok"))
    root = await bot.root_session(user)
    asking = await bot.create_session(user, parent=root, title="Asks")
    engine._client.responses = [say("Which day?", kind="question")]
    await bot.ask(asking, "Plan dinner")
    busy = await bot.create_session(user, parent=root, title="Busy")
    await Worker.objects.acreate(
        session=busy, bot_name="kitchen", title="build", function="x", status="running"
    )
    done = await bot.create_session(user, parent=root, title="Done")

    engine._client.responses = [
        claude_tool("ergo_thread_resolve", {"thread_id": str(asking.id)}, tool_id="q1"),
        claude_tool("ergo_thread_resolve", {"thread_id": str(busy.id)}, tool_id="q2"),
        claude_tool(
            "ergo_thread_resolve",
            {"thread_id": str(done.id), "summary": "PR #88 merged"},
            tool_id="q3",
        ),
        claude_tool("ergo_thread_resolve", {}, tool_id="q4"),  # main can't
        say("Tidied."),
    ]
    await bot.ask(root, "Tidy up")
    q1, q2, q3, q4 = _tool_results(engine)[-4:]
    assert q1["is_error"] and "asks the user something" in q1["content"]
    assert q2["is_error"] and "1 worker(s) are running" in q2["content"]
    assert q3["content"].startswith("Resolved kitchen · Done")
    assert q4["is_error"] and "only threads are" in q4["content"]
    await done.arefresh_from_db()
    assert done.status == "completed"
    assert done.metadata["resolved_summary"] == "PR #88 merged"
    assert done.metadata["resolved_by"] == "kitchen · Main"

    # A thread resolves itself when its task is done, during its final turn.
    engine._client.responses = [
        claude_tool("ergo_thread_resolve", {"summary": "answered"}, tool_id="s1"),
        say("Done, and resolved."),
    ]
    await bot.ask(asking, "Tuesday. That's all.")
    await asking.arefresh_from_db()
    assert asking.status == "completed"
    # A new message reopens it (Ergonaut's message endpoint and thread-message
    # delivery call archival.reopen), and the resolution notes go.
    from django_ergo.bots import archival

    assert await sync_to_async(archival.reopen)(asking)
    await asking.arefresh_from_db()
    assert asking.status == "active" and "resolved_summary" not in asking.metadata

    status = await sync_to_async(orchestrator.thread_status)(busy)
    assert status["ready_to_resolve"] is False


# ---------------------------------------------------------------------------
# Library skills and seeded tools
# ---------------------------------------------------------------------------

LIBRARY_YAML = """
    name: builder
    engine: {type: claude, config: {model: claude-test}}
    tools: [tools/pantry.py]
    skills: {include: [skillbuilder]}
"""


def test_library_skills_come_with_their_plugins(tmp_path):
    bot = Bot.load(write_bot(tmp_path, LIBRARY_YAML, name="builder"))
    skill = next(s for s in bot.skill_defs if s.name == "skillbuilder")
    assert skill.source == "ergo:skill_library/skillbuilder"
    assert skill.requires == ["config_repo", "introspection"]
    assert "ergo_config_repo_publish" in skill.instructions
    plugin = bot.plugin("bot_management")
    assert plugin is not None and plugin.mode == "propose_pr"
    assert "config_repo" in {s.name for s in bot.skill_defs}


def test_library_skills_load_when_required_and_respect_bot_yaml(tmp_path):
    folder = write_bot(
        tmp_path,
        """
        name: builder
        engine: {type: claude, config: {model: claude-test}}
        plugins: [{name: bot_management, approve_publish: false}]
        """,
        name="builder",
    )
    (folder / "skills").mkdir()
    (folder / "skills" / "tools-help.md").write_text(
        "---\nrequires: [skillbuilder]\n---\nBuild tools."
    )
    bot = Bot.load(folder)
    assert "skillbuilder" in {s.name for s in bot.skill_defs}
    plugin = bot.plugin("bot_management")
    assert plugin.mode == "propose_pr" and plugin.approve_publish is False

    (folder / "bot.yaml").write_text(
        textwrap.dedent(LIBRARY_YAML)
        + "plugins: [{name: bot_management, mode: merge_main}]\n"
    )
    with pytest.raises(ValueError, match="skillbuilder needs bot_management"):
        Bot.load(folder)

    # A skill in the bot's own folder wins over the library's.
    (folder / "bot.yaml").write_text(textwrap.dedent(LIBRARY_YAML))
    (folder / "skills" / "skillbuilder.md").write_text("Our own way.")
    bot = Bot.load(folder)
    ours = next(s for s in bot.skill_defs if s.name == "skillbuilder")
    assert ours.instructions == "Our own way."
    assert bot.plugin("bot_management") is None


def test_skills_without_library_names_get_no_library_skills(tmp_path):
    bot, _ = make_bot(tmp_path)
    assert not any(s.source.startswith("ergo:") for s in bot.skill_defs)


def test_seeded_bot_tools_pre_seed_their_results():
    @bot_tool(seed=True)
    def limits() -> str:
        """Account limits."""
        return "10 left"

    @bot_tool
    def other() -> str:
        return "no"

    toolkit = FunctionToolkit.from_functions([limits, other])
    seeds = toolkit.pre_seeds()
    assert [s.tool_name for s in seeds] == ["limits"]
    assert seeds[0].handler({}) == "10 left"
    with pytest.raises(ValueError, match="required parameters"):

        @bot_tool(seed=True)
        def needs(query: str) -> str:
            return query


def test_default_skills_reach_every_folder_bot_unless_it_opts_out(tmp_path, settings):
    settings.DJANGO_ERGO = {**settings.DJANGO_ERGO, "DEFAULT_SKILLS": ["skillbuilder"]}
    plain = Bot.load(
        write_bot(tmp_path, "name: plain\nengine: {type: claude}\n", name="plain")
    )
    assert "skillbuilder" in {s.name for s in plain.skill_defs}
    assert plain.plugin("bot_management").mode == "propose_pr"

    for name, skills in [
        ("out", "{exclude: [skillbuilder]}"),
        ("none", "{defaults: false}"),
    ]:
        bot = Bot.load(
            write_bot(tmp_path, f"name: {name}\nskills: {skills}\n", name=name)
        )
        assert "skillbuilder" not in {s.name for s in bot.skill_defs}
        assert bot.plugin("bot_management") is None

    # A default that clashes with bot.yaml is left out; a named one fails the load.
    clash = write_bot(
        tmp_path,
        "name: clash\nplugins: [{name: bot_management, mode: merge_main}]\n",
        name="clash",
    )
    bot = Bot.load(clash)
    assert "skillbuilder" not in {s.name for s in bot.skill_defs}
    assert bot.plugin("bot_management").mode == "merge_main"
    (clash / "bot.yaml").write_text(
        "name: clash\nplugins: [{name: bot_management, mode: merge_main}]\n"
        "skills: {include: [skillbuilder]}\n"
    )
    with pytest.raises(ValueError, match="skillbuilder needs bot_management"):
        Bot.load(clash)


@pytest.mark.django_db(transaction=True)
async def test_main_context_metadata_and_seeds_return_after_compaction(tmp_path):
    from django_ergo.conversation.compaction import compact_session
    from tests.test_conversation_compaction import RecordingSummarizer

    tools = TOOLS + textwrap.dedent(
        """
        @bot_tool(seed=True)
        def account_limits() -> str:
            '''Current account limits.'''
            return "10 left"
        """
    )
    user = await User.objects.acreate(username="context-seeds")
    bot, engine = make_bot(
        tmp_path, say("first"), say("second"), say("third"), tools=tools
    )
    session = await bot.main_session(user)
    first = await bot.ask(session, "one")
    context = first.call.metadata["context"]
    assert context["context_window"] == 200000
    assert context["compact_at_tokens"] == 150000
    assert context["compaction"] is None
    assert context["stubbed_results"] == 0
    assert any(s["title"] == "This chat" for s in context["sections"])
    assert "Recent messages in this conversation" not in str(context)
    assert first.call.metadata["seeded"]
    second = await bot.ask(session, "two")
    assert "seeded" not in second.call.metadata
    folded = await compact_session(
        session, engine, keep_tokens=0, summarizer=RecordingSummarizer()
    )
    third = await bot.ask(session, "three")
    assert third.call.metadata["seeded"]
    assert third.call.metadata["context"]["compaction"]["id"] == str(folded.pk)
    assert (
        third.call.metadata["context"]["native_messages"]["first_sequence"]
        > folded.upto_sequence
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


def test_files_note_lists_files_without_a_size():
    from types import SimpleNamespace

    from django_ergo.bots.messaging import files_note

    files = [
        {
            "id": "a",
            "filename": "plan.md",
            "media_type": "text/markdown",
            "size": 1200,
            "session": "s",
        },
        {
            "id": "b",
            "filename": "PR #14",
            "media_type": "text/uri-list",
            "size": None,
            "session": "s",
        },
    ]
    note = files_note(SimpleNamespace(metadata={"attachments": files}))
    assert "- plan.md (text/markdown, 1,200 bytes), id a" in note
    assert "- PR #14 (text/uri-list), id b" in note
