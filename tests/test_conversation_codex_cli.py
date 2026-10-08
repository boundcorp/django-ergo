from __future__ import annotations

import json
import shutil
import threading
from http.server import BaseHTTPRequestHandler
from http.server import ThreadingHTTPServer

import pytest

from django_ergo.bots.definition import BotDefinition
from django_ergo.bots.providers import Providers
from django_ergo.bots.registry import BotRegistry
from django_ergo.bots.runtime import Bot
from django_ergo.conversation.engines.codex_cli import CodexCLIEngine
from django_ergo.conversation.engines.codex_cli import CodexClient
from django_ergo.conversation.engines.codex_cli import CodexCLIError
from django_ergo.conversation.engines.codex_cli import child_env
from django_ergo.conversation.engines.codex_cli import dynamic_tools
from django_ergo.conversation.engines.codex_cli import response_items
from django_ergo.conversation.runtime import build_engine

HISTORY = [
    {"role": "system", "content": "You are a kitchen bot."},
    {"role": "user", "content": "find me a recipe"},
    {
        "role": "assistant",
        "content": "Looking.",
        "tool_calls": [
            {
                "id": "call_0",
                "type": "function",
                "function": {"name": "lookup", "arguments": '{"q": "soup"}'},
            }
        ],
    },
    {"role": "tool", "tool_call_id": "call_0", "content": "soup"},
    {"role": "user", "content": "and pasta?"},
]
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "lookup",
            "description": "Look up a recipe",
            "parameters": {"type": "object", "properties": {"q": {"type": "string"}}},
        },
    }
]


def test_history_becomes_responses_items():
    instructions, items = response_items(HISTORY)
    assert instructions == "You are a kitchen bot."
    assert [i["type"] for i in items] == [
        "message",
        "message",
        "function_call",
        "function_call_output",
        "message",
    ]
    assert items[1]["content"] == [{"type": "output_text", "text": "Looking."}]
    assert items[2] == {
        "type": "function_call",
        "call_id": "call_0",
        "name": "lookup",
        "arguments": '{"q": "soup"}',
    }
    assert items[3]["output"] == "soup"
    with pytest.raises(CodexCLIError, match="at least one message"):
        response_items(HISTORY[:1])


def test_tools_and_environment(monkeypatch):
    assert dynamic_tools(TOOLS) == [
        {
            "type": "function",
            "name": "lookup",
            "description": "Look up a recipe",
            "inputSchema": TOOLS[0]["function"]["parameters"],
        }
    ]
    monkeypatch.setenv("OPENAI_API_KEY", "sk-nope")
    monkeypatch.setenv("CODEX_HOME", "/home/me/.codex")
    env = child_env()
    assert "OPENAI_API_KEY" not in env
    assert env["CODEX_HOME"] == "/home/me/.codex"


def test_providers_and_bots_select_the_codex_transport(tmp_path):
    found = Providers.from_dict(
        {
            "default": "chatgpt/gpt-6-sol",
            "providers": {
                "chatgpt": {
                    "type": "openai",
                    "transport": "cli",
                    "config": {"command": shutil.which("sh") or "sh"},
                    "models": ["gpt-6-sol"],
                },
                "elsewhere": {
                    "type": "openai",
                    "transport": "cli",
                    "config": {"command": str(tmp_path / "missing")},
                    "models": ["gpt-6-luna"],
                },
            },
        }
    )
    assert found.providers["chatgpt"].available
    assert not found.providers["elsewhere"].available

    bot = Bot(BotDefinition.from_dict({"name": "kitchen"}))
    registry = BotRegistry()
    registry.providers = found
    registry.add(bot)
    spec = bot.engine_spec()
    assert (spec.engine_type, spec.transport_type) == ("openai", "cli")
    assert "api_key" not in spec.config
    assert isinstance(build_engine(spec), CodexCLIEngine)


async def test_missing_cli_names_the_problem(tmp_path):
    client = CodexClient(command=str(tmp_path / "nope"))
    with pytest.raises(CodexCLIError, match="isn't installed"):
        await client.chat.completions.create(model="gpt-6-sol", messages=HISTORY)


# -- The real CLI against a stand-in Responses API -----------------------------


class FakeResponses(BaseHTTPRequestHandler):
    requests: list[dict] = []

    def log_message(self, *args):
        pass

    def do_GET(self):
        self.send_response(404)
        self.end_headers()

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["content-length"])))
        self.requests.append(body)
        output = [
            {
                "type": "message",
                "id": "msg_1",
                "role": "assistant",
                "status": "completed",
                "content": [
                    {"type": "output_text", "text": "Checking.", "annotations": []}
                ],
            },
            *(
                {
                    "type": "function_call",
                    "id": f"fc_{n}",
                    "call_id": f"call_{n}",
                    "name": "lookup",
                    "arguments": json.dumps({"q": q}),
                    "status": "completed",
                }
                for n, q in ((1, "pasta"), (2, "pesto"))
            ),
        ]
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.end_headers()

        def event(kind, **data):
            payload = json.dumps({"type": kind, **data})
            self.wfile.write(f"event: {kind}\ndata: {payload}\n\n".encode())

        event("response.created", response={"id": "resp_1", "output": []})
        for index, item in enumerate(output):
            event("response.output_item.added", output_index=index, item=item)
            event("response.output_item.done", output_index=index, item=item)
        event(
            "response.completed",
            response={
                "id": "resp_1",
                "status": "completed",
                "output": output,
                "usage": {
                    "input_tokens": 100,
                    "input_tokens_details": {"cached_tokens": 40},
                    "output_tokens": 12,
                    "output_tokens_details": {"reasoning_tokens": 5},
                    "total_tokens": 112,
                },
            },
        )
        self.wfile.flush()


@pytest.mark.skipif(not shutil.which("codex"), reason="Codex CLI not installed")
async def test_real_codex_cli_makes_one_request(tmp_path):
    FakeResponses.requests = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeResponses)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    home = tmp_path / "codex-home"
    home.mkdir()
    try:
        engine = CodexCLIEngine(
            {
                "model": "gpt-6-sol",
                "codex_home": str(home),
                "effort": "high",
                "timeout": 30,
                "require_chatgpt": False,
                "codex_config": {
                    "model_provider": '"fake"',
                    "model_providers.fake": (
                        '{name = "fake", wire_api = "responses", '
                        f'base_url = "http://127.0.0.1:{server.server_port}/v1"}}'
                    ),
                },
            }
        )
        completion = await engine.complete(
            HISTORY[1:], system="You are a kitchen bot.", tools=TOOLS
        )
    finally:
        server.shutdown()

    calls = [e.tool_use for e in completion.events if e.event_type == "tool_use"]
    assert calls == [
        {"id": "call_1", "name": "lookup", "input": {"q": "pasta"}},
        {"id": "call_2", "name": "lookup", "input": {"q": "pesto"}},
    ]
    assert completion.message["content"] == "Checking."
    assert (completion.input_tokens, completion.cache_read_input_tokens) == (60, 40)
    assert (completion.output_tokens, completion.reasoning_tokens) == (12, 5)

    # One request: Codex stopped before running the tools itself.
    assert len(FakeResponses.requests) == 1
    body = FakeResponses.requests[0]
    assert body["model"] == "gpt-6-sol"
    assert body["reasoning"]["effort"] == "high"
    # gpt-6 models get the "lite" request: tools and instructions in the input.
    tools, *sent = body["input"]
    assert tools["type"] == "additional_tools"
    names = [t["name"] for ns in tools["tools"] for t in ns["tools"]]
    assert names == ["request_user_input", "lookup"]  # no shell, exec or agents
    assert [(i["type"], i.get("role")) for i in sent] == [
        ("message", "developer"),
        ("message", "user"),
        ("message", "assistant"),
        ("function_call", None),
        ("function_call_output", None),
        ("message", "user"),
    ]
    assert sent[0]["content"][0]["text"] == "You are a kitchen bot."
