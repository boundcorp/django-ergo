"""Request-only turn context and inspection metadata shared by both transports."""

from __future__ import annotations

from django_ergo.conversation.compaction import compaction_config
from django_ergo.conversation.compaction import estimate_message_tokens
from django_ergo.conversation.compaction import native_turn_start
from django_ergo.conversation.engine import session_system_prompt
from django_ergo.conversation.tool_results import trim_tool_results
from django_ergo.settings import api_settings


def prepare_turn_context(engine, session, messages, rows, compaction):
    """Trim a request copy, prepend turn context, and record what was sent."""
    sent_ids = {id(message) for message in messages}
    native = [(row, message) for row, message in rows if id(message) in sent_ids]
    stats = {}
    budget = engine.tool_results_tokens
    if budget is None:
        budget = api_settings.TOOL_RESULTS_TOKENS
    if budget is None:
        budget = int(engine.context_window * 0.2)
    messages = trim_tool_results(
        messages,
        keep=engine.tool_results_in_context,
        budget_tokens=budget,
        stats=stats,
    )
    if engine.ephemeral_context:
        start = native_turn_start(messages)
        if start is None:
            start = next(
                (i for i, m in enumerate(messages) if m.get("role") == "user"), None
            )
        if start is not None:
            message = messages[start]
            text = f"<turn-context>\n{engine.ephemeral_context}\n</turn-context>"
            content = message.get("content")
            if isinstance(content, list):
                content = [{"type": "text", "text": text}, *content]
            else:
                content = text + "\n\n" + (content or "")
            messages = list(messages)
            messages[start] = {**message, "content": content}
    engine.last_request_info = {
        "sections": getattr(engine, "context_sections", []),
        "compaction": {
            "id": str(compaction.pk),
            "upto_sequence": compaction.upto_sequence,
            "message_count": compaction.message_count,
            "reason": compaction.reason,
            "created_at": compaction.created_at.isoformat(),
        }
        if compaction
        else None,
        "native_messages": {
            "count": len(native),
            "first_sequence": native[0][0].sequence if native else None,
        },
        **stats,
        "estimated_tokens": sum(estimate_message_tokens(m) for m in messages)
        + (
            0
            if any(m.get("role") == "system" for m in messages)
            else (len(session_system_prompt(session)) + 3) // 4
        ),
        "context_window": engine.context_window,
        "compact_at_tokens": compaction_config(session, engine.context_window).get(
            "compact_at_tokens"
        ),
    }
    return messages
