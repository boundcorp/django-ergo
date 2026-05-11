"""Conversation pipelines — run a full chat history through a one-shot generative workflow.

Usage:
    summary = await summarize_conversation(session, engine)
    compacted = await compact_conversation(session, engine)

    # Custom pipeline:
    result = await run_conversation_pipeline(
        session, engine,
        system="Extract all action items from this conversation.",
        response_model=ActionItems,
    )
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel

if TYPE_CHECKING:
    from django_ergo.conversation.engine import Engine
    from django_ergo.conversation.engine import EngineResponse
    from django_ergo.conversation.models import ConversationSession

from django_ergo.conversation.renderer import ConversationRenderer
from django_ergo.conversation.runtime import generate_once


def _format_conversation_as_text(messages: list[dict]) -> str:
    """Deprecated: use ConversationRenderer(detail='full').render_messages() instead."""
    return ConversationRenderer(detail="full").render_messages(messages)


async def run_conversation_pipeline(
    session: ConversationSession,
    engine: Engine,
    system: str,
    response_model: type | None = None,
    renderer: ConversationRenderer | None = None,
) -> EngineResponse:
    """Run a conversation through a one-shot generative pipeline.

    Uses ConversationRenderer for token-efficient formatting.
    Defaults to skeleton detail level.

    Args:
        session: The conversation to process.
        engine: Engine to use for generation.
        system: System prompt describing what to do with the conversation.
        response_model: Optional Pydantic model for structured output.
        renderer: Optional ConversationRenderer to customize transcript formatting.
                 Defaults to skeleton detail level if not provided.

    Returns:
        EngineResponse with text or parsed structured output in raw["parsed"].
    """
    if renderer is None:
        renderer = ConversationRenderer(detail="skeleton")

    messages = engine.reconstruct_messages(session)
    transcript = renderer.render_messages(messages)

    prompt = f"Here is a conversation transcript:\n\n{transcript}"

    return await generate_once(
        prompt=prompt,
        engine=engine,
        system=system,
        response_model=response_model,
    )


# ---------------------------------------------------------------------------
# Pre-built pipelines
# ---------------------------------------------------------------------------

SUMMARIZE_SYSTEM = """\
You are a conversation summarizer. Given a conversation transcript, produce a \
clear, concise summary that captures:
- The main topic(s) discussed
- Key decisions made
- Important information exchanged
- Any action items or next steps
- The overall outcome

Be concise but thorough. Write in prose, not bullet points."""


async def summarize_conversation(
    session: ConversationSession,
    engine: Engine,
) -> str:
    """Generate a text summary of a conversation.

    Returns the summary text.
    """
    response = await run_conversation_pipeline(session, engine, system=SUMMARIZE_SYSTEM)
    return response.text or ""


class CompactedMessage(BaseModel):
    """A single message in a compacted conversation."""

    role: str
    content: str


class CompactedConversation(BaseModel):
    """A compacted version of a conversation — key exchanges only."""

    title: str
    messages: list[CompactedMessage]
    context_notes: str


COMPACT_SYSTEM = """\
You are a conversation compactor. Given a conversation transcript, produce a \
compacted version that preserves the essential context needed to continue the \
conversation, while removing:
- Redundant back-and-forth
- Tool call/result details (summarize what the tool found instead)
- Thinking blocks
- Pleasantries and filler

The compacted conversation should be short enough to fit in a single context \
window while retaining all information needed for an AI to seamlessly continue \
the conversation.

Include a title summarizing the conversation topic, and context_notes with any \
important background that isn't captured in the messages themselves."""


async def compact_conversation(
    session: ConversationSession,
    engine: Engine,
) -> CompactedConversation:
    """Compact a conversation to its essential exchanges.

    Returns a CompactedConversation with title, key messages, and context notes.
    """
    response = await run_conversation_pipeline(
        session, engine, system=COMPACT_SYSTEM, response_model=CompactedConversation
    )
    return response.raw["parsed"]
