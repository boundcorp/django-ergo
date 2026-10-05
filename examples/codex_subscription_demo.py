"""Run a two-step tool loop on a ChatGPT subscription through the Codex CLI.

Needs the Codex CLI installed and logged in with ChatGPT (``codex login``)::

    python examples/codex_subscription_demo.py
    python examples/codex_subscription_demo.py --model gpt-6-luna --effort low

The model is asked a question it can only answer with the ``kitchen_stock``
tool. The demo runs the tool itself and makes a second call with the
result, the same loop an Ergo bot turn runs, then prints the answer, the
token usage of each call and the subscription's rate limit windows.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time

import django
from django.conf import settings

settings.configure(
    INSTALLED_APPS=["django.contrib.auth", "django.contrib.contenttypes"],
    DATABASES={},
)
django.setup()

from django_ergo.conversation.engines.codex_cli import CodexCLIEngine  # noqa: E402

STOCK = {"basil": "2 bunches", "pine nuts": "none", "parmesan": "300 g"}
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "kitchen_stock",
            "description": "How much of an ingredient the kitchen has.",
            "parameters": {
                "type": "object",
                "properties": {"ingredient": {"type": "string"}},
                "required": ["ingredient"],
            },
        },
    }
]


def windows(rate_limits: dict | None) -> str:
    lines = []
    for name in ("primary", "secondary"):
        window = (rate_limits or {}).get(name)
        if window:
            resets = window.get("resetsAt")
            left = f", resets in {(resets - time.time()) / 3600:.1f}h" if resets else ""
            mins = window.get("windowDurationMins")
            lines.append(
                f"  {name} ({mins} min window): {window['usedPercent']}% used{left}"
            )
    return "\n".join(lines) or "  (none reported)"


async def main(args) -> None:
    config = {"model": args.model, "effort": args.effort, "timeout": 120}
    if args.provider_url:  # a stand-in Responses API, for testing without a login
        config["require_chatgpt"] = False
        config["codex_config"] = {
            "model_provider": '"fake"',
            "model_providers.fake": (
                f'{{name = "fake", wire_api = "responses", base_url = "{args.provider_url}"}}'
            ),
        }
    engine = CodexCLIEngine(config)
    client = engine.client
    messages = [
        {
            "role": "system",
            "content": "You are a kitchen assistant. Check stock with tools.",
        },
        {"role": "user", "content": "Can I make pesto tonight? Check each ingredient."},
    ]
    for step in range(1, 9):
        started = time.monotonic()
        response = await client.chat.completions.create(
            model=args.model, messages=messages, tools=TOOLS
        )
        message = response.choices[0].message
        usage = response.usage
        print(
            f"call {step}: {time.monotonic() - started:.1f}s, "
            f"{usage.prompt_tokens} in ({usage.prompt_tokens_details['cached_tokens']} cached), "
            f"{usage.completion_tokens} out"
        )
        entry = {"role": "assistant", "content": message.content}
        if message.tool_calls:
            entry["tool_calls"] = [call.model_dump() for call in message.tool_calls]
        messages.append(entry)
        if not message.tool_calls:
            print(f"\nanswer: {message.content}")
            break
        for call in message.tool_calls:
            ingredient = json.loads(call.function.arguments).get("ingredient", "")
            result = next(
                (have for name, have in STOCK.items() if name in ingredient.lower()),
                "none",
            )
            print(f"  tool kitchen_stock({ingredient!r}) -> {result}")
            messages.append(
                {"role": "tool", "tool_call_id": call.id, "content": result}
            )
    print("\nsubscription windows:\n" + windows(response.usage_windows))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--model", default="gpt-6-sol")
    parser.add_argument("--effort", default="medium")
    parser.add_argument("--provider-url", default="", help=argparse.SUPPRESS)
    asyncio.run(main(parser.parse_args()))
