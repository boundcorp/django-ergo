from __future__ import annotations

import json
import shutil
import sys
import threading
from http.server import BaseHTTPRequestHandler
from http.server import ThreadingHTTPServer

import pytest

from django_ergo.bots.definition import BotDefinition
from django_ergo.bots.providers import Providers
from django_ergo.bots.providers import ProvidersError
from django_ergo.bots.registry import BotRegistry
from django_ergo.bots.runtime import Bot
from django_ergo.conversation.engines.claude_code import ClaudeCodeClient
from django_ergo.conversation.engines.claude_code import ClaudeCodeEngine
from django_ergo.conversation.engines.claude_code import ClaudeCodeError
from django_ergo.conversation.engines.claude_code import _cut_denials
from django_ergo.conversation.engines.claude_code import child_env
from django_ergo.conversation.engines.claude_code import cli_frames
from django_ergo.conversation.engines.claude_code import history_keys
from django_ergo.conversation.engines.claude_code import mcp_tools
from django_ergo.conversation.runtime import build_engine

HISTORY = [
    {"role": "user", "content": "find me a recipe"},
    {
        "role": "assistant",
        "content": [
            {"type": "thinking", "thinking": "they want soup"},
            {"type": "text", "text": "Looking."},
            {
                "type": "tool_use",
                "id": "toolu_0",
                "name": "lookup",
                "input": {"q": "soup"},
            },
        ],
    },
    {
        "role": "user",
        "content": [
            {"type": "tool_result", "tool_use_id": "toolu_0", "content": "soup"}
        ],
    },
    {"role": "user", "content": [{"type": "text", "text": "and pasta?"}]},
]
TOOLS = [
    {
        "name": "lookup",
        "description": "Look up a recipe",
        "input_schema": {"type": "object", "properties": {"q": {"type": "string"}}},
    }
]


def test_history_becomes_stream_json_frames():
    frames = cli_frames(HISTORY)
    assert [f["type"] for f in frames] == ["user", "assistant", "user"]
    assert frames[0]["shouldQuery"] is False
    assert "shouldQuery" not in frames[2]  # the last frame queries
    # Thinking (unsigned) is dropped; tool calls use the MCP name.
    assert frames[1]["message"]["content"] == [
        {"type": "text", "text": "Looking."},
        {
            "type": "tool_use",
            "id": "toolu_0",
            "name": "mcp__ergo__lookup",
            "input": {"q": "soup"},
        },
    ]
    # Consecutive user messages merge into one frame.
    assert [b["type"] for b in frames[2]["message"]["content"]] == [
        "tool_result",
        "text",
    ]
    with pytest.raises(ClaudeCodeError, match="ends with a user message"):
        cli_frames(HISTORY[:2])


def test_tools_and_environment():
    assert mcp_tools(TOOLS) == [
        {
            "name": "lookup",
            "description": "Look up a recipe",
            "inputSchema": {"type": "object", "properties": {"q": {"type": "string"}}},
        }
    ]
    with pytest.raises(ClaudeCodeError, match="longer than"):
        mcp_tools([{"name": "x" * 60}])


def test_child_environment_keeps_only_the_subscription_login(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://gateway")
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("CLAUDE_CODE_USE_BEDROCK", "1")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/srv/claude")
    env = child_env({"CLAUDE_CODE_MAX_OUTPUT_TOKENS": "900"})
    for key in (
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_BASE_URL",
        "CLAUDECODE",
        "CLAUDE_CODE_USE_BEDROCK",
    ):
        assert key not in env
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "sk-ant-oat"
    assert env["CLAUDE_CONFIG_DIR"] == "/srv/claude"
    assert env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] == "900"
    assert env["DISABLE_AUTO_COMPACT"] == "1"


FAKE_CLI = """#!{python}
import json, sys, time
log = open({log!r}, "a")
log.write(json.dumps(sys.argv[1:]) + "\\n")
def out(event):
    sys.stdout.write(json.dumps(event) + "\\n"); sys.stdout.flush()
def stream(event):
    out({{"type": "stream_event", "event": event}})
out({{"type": "system", "subtype": "init"}})
for line in sys.stdin:
    frame = json.loads(line)
    log.write(line)
    log.flush()
    if frame.get("shouldQuery") is False:
        out({{"type": "result", "subtype": "success", "num_turns": 0, "is_error": False}})
    elif frame["type"] == "user":
        break
if {mode!r} == "logged_out":
    out({{"type": "assistant", "error": "authentication_failed",
         "message": {{"content": [{{"type": "text", "text": "Not logged in"}}]}}}})
    out({{"type": "result", "subtype": "success", "is_error": True, "result": "Not logged in"}})
    sys.exit(1)
stream({{"type": "message_start", "message": {{"model": "claude-sonnet-5-5",
        "usage": {{"input_tokens": 100, "output_tokens": 1, "cache_read_input_tokens": 40,
                  "cache_creation_input_tokens": 7}}}}}})
stream({{"type": "content_block_start", "index": 0, "content_block": {{"type": "thinking", "thinking": ""}}}})
stream({{"type": "content_block_delta", "index": 0, "delta": {{"type": "thinking_delta", "thinking": "pasta"}}}})
stream({{"type": "content_block_stop", "index": 0}})
stream({{"type": "content_block_start", "index": 1, "content_block": {{"type": "text", "text": ""}}}})
stream({{"type": "content_block_delta", "index": 1, "delta": {{"type": "text_delta", "text": "One "}}}})
stream({{"type": "content_block_delta", "index": 1, "delta": {{"type": "text_delta", "text": "moment."}}}})
stream({{"type": "content_block_stop", "index": 1}})
stream({{"type": "content_block_start", "index": 2, "content_block": {{"type": "tool_use", "id": "toolu_1",
        "name": "mcp__ergo__lookup", "input": {{}}}}}})
stream({{"type": "content_block_delta", "index": 2, "delta": {{"type": "input_json_delta", "partial_json": "{{\\"q\\": "}}}})
stream({{"type": "content_block_delta", "index": 2, "delta": {{"type": "input_json_delta", "partial_json": "\\"pasta\\"}}"}}}})
stream({{"type": "content_block_stop", "index": 2}})
stream({{"type": "message_delta", "delta": {{"stop_reason": "tool_use"}}, "usage": {{"output_tokens": 12}}}})
stream({{"type": "message_stop"}})
time.sleep(60)  # a second request would start here; the engine stops the process first
"""


def fake_cli(tmp_path, mode="tool"):
    log = tmp_path / "cli.log"
    script = tmp_path / "claude"
    script.write_text(FAKE_CLI.format(python=sys.executable, log=str(log), mode=mode))
    script.chmod(0o755)
    return str(script), log


async def test_engine_completes_one_model_call_through_the_cli(tmp_path):
    command, log = fake_cli(tmp_path)
    engine = ClaudeCodeEngine(
        {
            "model": "claude-sonnet-5-5",
            "max_tokens": 900,
            "command": command,
            "effort": "high",
            "timeout": 20,
        }
    )
    completion = await engine.complete(
        HISTORY, system="You are a kitchen bot.", tools=TOOLS
    )

    assert completion.message == {
        "role": "assistant",
        "content": [
            {"type": "thinking", "thinking": "pasta"},
            {"type": "text", "text": "One moment."},
            {
                "type": "tool_use",
                "id": "toolu_1",
                "name": "lookup",
                "input": {"q": "pasta"},
            },
        ],
    }
    calls = [e.tool_use for e in completion.events if e.event_type == "tool_use"]
    assert calls == [{"id": "toolu_1", "name": "lookup", "input": {"q": "pasta"}}]
    assert (completion.input_tokens, completion.output_tokens) == (100, 12)
    assert (
        completion.cache_read_input_tokens,
        completion.cache_creation_input_tokens,
    ) == (40, 7)

    lines = log.read_text().splitlines()
    args = json.loads(lines[0])
    assert args[args.index("--model") + 1] == "claude-sonnet-5-5"
    assert args[args.index("--effort") + 1] == "high"
    assert args[args.index("--permission-mode") + 1] == "dontAsk"
    assert args[args.index("--tools") + 1] == ""
    assert [json.loads(line)["type"] for line in lines[1:]] == [
        "user",
        "assistant",
        "user",
    ]


async def test_cli_errors_name_the_problem(tmp_path):
    command, _ = fake_cli(tmp_path, mode="logged_out")
    client = ClaudeCodeClient(command=command, timeout=20)
    with pytest.raises(ClaudeCodeError, match="authentication_failed: Not logged in"):
        await client.messages.create(model="sonnet", messages=HISTORY[:1])
    missing = ClaudeCodeClient(command=str(tmp_path / "nope"))
    with pytest.raises(ClaudeCodeError, match="isn't installed"):
        await missing.messages.create(model="sonnet", messages=HISTORY[:1])


def test_providers_and_bots_select_the_cli_transport(tmp_path, monkeypatch):
    command, _ = fake_cli(tmp_path)
    found = Providers.from_dict(
        {
            "default": "subscription/claude-sonnet-5-5",
            "providers": {
                "subscription": {
                    "type": "claude",
                    "transport": "cli",
                    "config": {"command": command},
                    "models": ["claude-sonnet-5-5"],
                },
                "elsewhere": {
                    "type": "claude",
                    "transport": "cli",
                    "config": {"command": str(tmp_path / "missing")},
                    "models": ["claude-haiku-4-5"],
                },
            },
        }
    )
    assert found.providers["subscription"].available
    assert not found.providers["elsewhere"].available
    with pytest.raises(ProvidersError, match="doesn't work with openai"):
        Providers.from_dict(
            {"providers": {"openai": {"transport": "grpc", "models": ["g"]}}}
        )

    bot = Bot(BotDefinition.from_dict({"name": "kitchen"}))
    registry = BotRegistry()
    registry.providers = found
    registry.add(bot)
    spec = bot.engine_spec()
    assert (spec.engine_type, spec.transport_type) == ("claude", "cli")
    assert "api_key" not in spec.config
    assert isinstance(build_engine(spec), ClaudeCodeEngine)

    # Without providers.yaml, bot.yaml can ask for it directly.
    plain = Bot(
        BotDefinition.from_dict(
            {
                "name": "plain",
                "engine": {
                    "type": "claude",
                    "transport": "cli",
                    "config": {"model": "sonnet"},
                },
            }
        )
    )
    assert plain.engine_spec().transport_type == "cli"


# -- The real CLI against a stand-in Anthropic API ----------------------------


def sse(event: dict) -> bytes:
    return f"event: {event['type']}\ndata: {json.dumps(event)}\n\n".encode()


class FakeAnthropic(BaseHTTPRequestHandler):
    requests: list[dict] = []

    def log_message(self, *args):
        pass

    def do_GET(self):
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.end_headers()
        self.wfile.write(b"{}")

    def do_POST(self):
        body = json.loads(
            self.rfile.read(int(self.headers.get("content-length", 0))) or b"{}"
        )
        if "count_tokens" in self.path:
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"input_tokens": 10}')
            return
        self.requests.append(body)
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.end_headers()
        tool = next(t["name"] for t in body["tools"] if t["name"].endswith("lookup"))
        events = [
            {
                "type": "message_start",
                "message": {
                    "id": "msg_1",
                    "type": "message",
                    "role": "assistant",
                    "model": body["model"],
                    "content": [],
                    "stop_reason": None,
                    "usage": {"input_tokens": 100, "output_tokens": 1},
                },
            },
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "text", "text": ""},
            },
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": "Checking."},
            },
            {"type": "content_block_stop", "index": 0},
            {
                "type": "content_block_start",
                "index": 1,
                "content_block": {
                    "type": "tool_use",
                    "id": f"toolu_{len(self.requests)}",
                    "name": tool,
                    "input": {},
                },
            },
            {
                "type": "content_block_delta",
                "index": 1,
                "delta": {"type": "input_json_delta", "partial_json": '{"q": "pasta"}'},
            },
            {"type": "content_block_stop", "index": 1},
            {
                "type": "message_delta",
                "delta": {"stop_reason": "tool_use"},
                "usage": {"output_tokens": 9},
            },
            {"type": "message_stop"},
        ]
        for event in events:
            self.wfile.write(sse(event))


@pytest.mark.skipif(
    shutil.which("claude") is None, reason="Claude Code isn't installed"
)
async def test_real_claude_code_cli_makes_one_request(tmp_path):
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeAnthropic)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    FakeAnthropic.requests = []
    try:
        engine = ClaudeCodeEngine({"model": "claude-sonnet-5-5", "timeout": 60})
        engine._client = ClaudeCodeClient(
            timeout=60,
            env={
                "HOME": str(tmp_path),
                "CLAUDE_CONFIG_DIR": str(tmp_path / ".claude"),
                "ANTHROPIC_BASE_URL": f"http://127.0.0.1:{server.server_port}",
                "ANTHROPIC_API_KEY": "sk-ant-test",
            },
        )
        completion = await engine.complete(
            HISTORY, system="You are a kitchen bot.", tools=TOOLS
        )
    finally:
        server.shutdown()

    assert [e.tool_use for e in completion.events if e.event_type == "tool_use"] == [
        {"id": "toolu_1", "name": "lookup", "input": {"q": "pasta"}}
    ]
    (request,) = FakeAnthropic.requests
    assert [t["name"] for t in request["tools"]] == ["mcp__ergo__lookup"]
    assert request["tools"][0]["description"] == "Look up a recipe"
    assert any("You are a kitchen bot." in block["text"] for block in request["system"])
    replayed = [m for m in request["messages"] if m["role"] != "system"]
    assert [m["role"] for m in replayed] == ["user", "assistant", "user"]
    assert replayed[1]["content"][1]["name"] == "mcp__ergo__lookup"


@pytest.mark.skipif(
    shutil.which("claude") is None, reason="Claude Code isn't installed"
)
async def test_real_claude_code_cli_resumes_its_transcript(tmp_path, monkeypatch):
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeAnthropic)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    FakeAnthropic.requests = []
    client = ClaudeCodeClient(
        timeout=60,
        env={
            "HOME": str(tmp_path),
            "CLAUDE_CONFIG_DIR": str(tmp_path / ".claude"),
            "ANTHROPIC_BASE_URL": f"http://127.0.0.1:{server.server_port}",
            "ANTHROPIC_API_KEY": "sk-ant-test",
        },
    )
    runs = []
    run = client._run

    async def spy(args, frames, env, model):
        runs.append((args, frames))
        return await run(args, frames, env, model)

    monkeypatch.setattr(client, "_run", spy)
    first = {
        "role": "user",
        "content": [
            {"type": "text", "text": "<turn-context>\nMonday\n</turn-context>"},
            {"type": "text", "text": "find me pasta"},
        ],
    }
    call = {"model": "claude-sonnet-5-5", "system": "Kitchen bot.", "tools": TOOLS}
    try:
        reply = await client.messages.create(messages=[first], **call)
        answer = {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "Checking."},
                {
                    "type": "tool_use",
                    "id": "toolu_1",
                    "name": "lookup",
                    "input": {"q": "pasta"},
                },
            ],
        }
        assert [b.type for b in reply.content] == ["text", "tool_use"]
        result = {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "toolu_1",
                    "content": "carbonara",
                }
            ],
        }
        await client.messages.create(messages=[first, answer, result], **call)
        # The next turn sends the first message without its turn context.
        again = json.loads(json.dumps(answer).replace("toolu_1", "toolu_2"))
        later = [
            {"role": "user", "content": [first["content"][1]]},
            answer,
            result,
            again,
            json.loads(json.dumps(result).replace("toolu_1", "toolu_2")),
        ]
        await client.messages.create(messages=later, **call)
        # A changed history starts over.
        await client.messages.create(
            messages=[{"role": "user", "content": "something else"}], **call
        )
    finally:
        server.shutdown()

    flags = [[a for a in args if a in ("--resume", "--session-id")] for args, _ in runs]
    assert flags == [["--session-id"], ["--resume"], ["--resume"], ["--session-id"]]
    # Resuming sends only what's new.
    assert [len(frames) for _, frames in runs] == [1, 1, 1, 1]
    sent = FakeAnthropic.requests[1]["messages"]
    results = [
        block
        for message in sent
        if isinstance(message["content"], list)
        for block in message["content"]
        if block.get("type") == "tool_result"
    ]
    # Ergo's result, not the CLI's "permission denied".
    assert len(results) == 1
    assert "carbonara" in json.dumps(results[0])
    # Each request starts with the one before it, so it reads from the cache.
    for before, after in zip(
        FakeAnthropic.requests[:2], FakeAnthropic.requests[1:3], strict=True
    ):
        old = [m for m in before["messages"] if m["role"] != "system"]
        new = [m for m in after["messages"] if m["role"] != "system"]
        assert json.dumps(new[0]) == json.dumps(old[0])


def test_history_keys_follow_what_the_cli_sees():
    keys = history_keys(HISTORY, "fp")
    assert len(keys) == len(HISTORY) + 1
    assert keys == history_keys(HISTORY, "fp")
    assert keys[1:] != history_keys(HISTORY, "other")[1:]
    # Thinking and cache markers aren't part of what the CLI is sent.
    plain = [HISTORY[0], {**HISTORY[1], "content": HISTORY[1]["content"][1:]}]
    assert history_keys(plain, "fp")[2] == keys[2]
    framed = {
        "role": "user",
        "content": "<turn-context>\nMonday\n</turn-context>\n\nfind me a recipe",
    }
    assert history_keys([framed], "fp")[1] != keys[1]
    assert history_keys([framed], "fp", strip_context=True)[1] == keys[1]


def test_denials_are_cut_from_the_transcript(tmp_path):
    rows = [
        {"type": "user", "uuid": "u1", "parentUuid": None},
        {"type": "assistant", "uuid": "a1", "parentUuid": "u1"},
        {
            "type": "user",
            "uuid": "d1",
            "parentUuid": "a1",
            "toolDenialKind": "mode",
            "sourceToolAssistantUUID": "a1",
            "message": {
                "content": [{"type": "tool_result", "tool_use_id": "t1"}],
            },
        },
        {"type": "assistant", "uuid": "a2", "parentUuid": "d1"},
        {"type": "attachment", "uuid": "x", "parentUuid": "a2"},
    ]
    path = tmp_path / "s.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))
    assert _cut_denials(path, ["a1", "a2"], {"t1"})
    kept = [json.loads(line) for line in path.read_text().splitlines()]
    assert [(r["uuid"], r["parentUuid"]) for r in kept] == [
        ("u1", None),
        ("a1", "u1"),
        ("a2", "a1"),
        ("x", "a2"),
    ]
    # A result the CLI recorded some other way means the transcript can't be used.
    rows[2].pop("toolDenialKind")
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))
    assert not _cut_denials(path, ["a1", "a2"], {"t1"})


def test_the_api_engine_drops_unsigned_thinking():
    from django_ergo.conversation.engines.claude_api import without_unsigned_thinking

    sent = without_unsigned_thinking(HISTORY)
    assert [b["type"] for b in sent[1]["content"]] == ["text", "tool_use"]
    signed = [
        {
            "role": "assistant",
            "content": [{"type": "thinking", "thinking": "x", "signature": "s"}],
        }
    ]
    assert without_unsigned_thinking(signed) == signed


def test_a_refusal_keeps_its_category():
    from django_ergo.conversation.engines.claude_api import done_raw
    from django_ergo.conversation.engines.claude_code import _Stream

    stream = _Stream()
    stream.feed({"type": "message_start", "message": {"usage": {}}})
    stream.feed(
        {
            "type": "message_delta",
            "delta": {"stop_reason": "refusal", "stop_details": {"category": "cyber"}},
            "usage": {"output_tokens": 0},
        }
    )
    stream.feed({"type": "message_stop"})
    response = stream.response("claude-opus-5-5")
    assert response.content == []
    assert done_raw(response) == {"stop_reason": "refusal", "refusal_category": "cyber"}
