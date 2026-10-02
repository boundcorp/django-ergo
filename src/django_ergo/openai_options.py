"""Request options for OpenAI Chat Completions that differ by model family.

Reasoning models (GPT-5, GPT-6 and the o-series) take ``max_completion_tokens``
and reject a custom ``temperature``. GPT-6 models only allow function calling
over Chat Completions with ``reasoning_effort="none"``, so that is sent
whenever tools are.
"""

from __future__ import annotations

DEFAULT_OPENAI_MODEL = "gpt-6-luna"

REASONING_PREFIXES = ("gpt-5", "gpt-6", "o1", "o3", "o4")
# Families that need reasoning_effort="none" for Chat Completions tool calls.
NO_REASONING_FOR_TOOLS = ("gpt-6",)


def is_reasoning_model(model: str) -> bool:
    return (model or "").startswith(REASONING_PREFIXES)


def chat_options(
    model: str,
    *,
    temperature: float | None = None,
    max_tokens: int | None = None,
    reasoning_effort: str | None = None,
    tools: bool = False,
) -> dict:
    """Keyword arguments for chat.completions.create besides model and messages."""
    options: dict = {}
    if max_tokens:
        options["max_completion_tokens"] = max_tokens
    if not is_reasoning_model(model):
        if temperature is not None:
            options["temperature"] = temperature
        return options
    if tools and model.startswith(NO_REASONING_FOR_TOOLS):
        reasoning_effort = "none"
    if reasoning_effort:
        options["reasoning_effort"] = reasoning_effort
    return options
