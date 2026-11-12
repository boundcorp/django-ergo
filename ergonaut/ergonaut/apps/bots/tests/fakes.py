"""A fake Claude client so tests can run bot turns without the API."""

import itertools
import json
from types import SimpleNamespace

from django_ergo.bots.registry import BotRegistry
from django_ergo.bots.runtime import Bot
from django_ergo.conversation.engines.claude_api import ClaudeAPIEngine

_ids = itertools.count(1)


def _usage(input_tokens=10, output_tokens=5):
    return SimpleNamespace(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_creation_input_tokens=0,
        cache_read_input_tokens=0,
    )


def tool_call(name, tool_input, *, input_tokens=10, output_tokens=5):
    block = SimpleNamespace(type="tool_use", id=f"toolu_{next(_ids)}", name=name, input=tool_input)
    return SimpleNamespace(content=[block], stop_reason="tool_use", usage=_usage(input_tokens, output_tokens))


def say(text, suggestions=None, kind="message", status=None):
    reply = {"type": kind, "text": text, "suggestions": suggestions or []}
    if status is not None:
        reply["status"] = status
    return tool_call("send_reply", reply)


class FakeClaude:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []
        self.options = []  # what with_options was given: a budgeted request is sent with some
        self.messages = self

    def with_options(self, **options):
        self.options.append(options)
        return self

    async def create(self, **kwargs):
        self.calls.append(json.loads(json.dumps(kwargs, default=str)))
        return self.responses.pop(0)


def fake_registry(folder, *responses):
    client = FakeClaude(*responses)

    def engine():
        made = ClaudeAPIEngine(config={"model": "claude-test", "max_tokens": 512})
        made._client = client
        return made

    registry = BotRegistry()
    registry.add(Bot.load(folder, engine_factory=engine))
    return registry, client
