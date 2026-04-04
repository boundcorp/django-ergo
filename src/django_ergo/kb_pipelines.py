"""KB pipelines — knowledge base operations driven by conversation analysis."""

from __future__ import annotations

from typing import TYPE_CHECKING

from asgiref.sync import sync_to_async

from django_ergo.conversation.renderer import ConversationRenderer
from django_ergo.conversation.runner import run_conversation_turn
from django_ergo.kb_suggest_toolkit import KBSuggestToolkit
from django_ergo.kb_toolkit import KBToolkit

if TYPE_CHECKING:
    from django_ergo.conversation.engine import Engine
    from django_ergo.conversation.models import ConversationSession
    from django_ergo.models import Knowledgebase

ABSORB_SYSTEM = """\
You are a knowledge base curator. You are reviewing a conversation to extract \
knowledge worth preserving into the knowledge base described below.

Knowledge Base: {kb_name}
Description: {kb_description}

Current table of contents:
{kb_toc}

Your job:
- Identify facts, decisions, preferences, and context from the conversation \
that belong in this knowledge base
- Use kb_suggest_create to propose new articles for new topics
- Use kb_suggest_update to propose improvements to existing articles
- Use kb_suggest_delete if conversation reveals an article is outdated or wrong
- Only propose changes relevant to this KB's description
- Avoid duplicating information already in the KB
- Use the KB read tools to check existing content before suggesting updates

Be selective. Not everything in a conversation belongs in a knowledge base."""


async def absorb_conversation(
    session: ConversationSession,
    target_kb: Knowledgebase,
    engine: Engine,
    system: str | None = None,
    renderer: ConversationRenderer | None = None,
) -> KBSuggestToolkit:
    """Review a conversation and propose KB changes.

    Renders the conversation, runs an absorption agent that calls
    KBSuggestToolkit tools, and returns the toolkit with accumulated
    suggestions for human review.

    Args:
        session: The source conversation to absorb from.
        target_kb: The knowledge base to propose changes for.
        engine: Engine to power the absorption agent.
        system: Custom system prompt. If None, uses ABSORB_SYSTEM with KB context.
        renderer: Custom renderer for the source conversation. Defaults to skeleton.

    Returns:
        KBSuggestToolkit with accumulated suggestions. Call
        get_suggestions() to review, apply_suggestions() to apply.
    """
    from django_ergo.conversation.models import ConversationSession as SessionModel

    # Render the source conversation (wrap in sync_to_async for DB safety)
    if renderer is None:
        renderer = ConversationRenderer(detail="skeleton")
    transcript = await sync_to_async(renderer.render)(session)

    # Build the prompt
    if system is None:
        toc = await sync_to_async(target_kb.get_table_of_contents)()
        system_text = ABSORB_SYSTEM.format(
            kb_name=target_kb.name,
            kb_description=target_kb.description,
            kb_toc=toc if toc else "(empty \u2014 no articles yet)",
        )
    else:
        system_text = system

    message = f"{system_text}\n\n---\n\nConversation to review:\n\n{transcript}"

    # Create toolkits
    suggest_toolkit = KBSuggestToolkit(knowledgebase=target_kb)
    read_toolkit = KBToolkit(knowledgebases=[target_kb])
    toolkits = [suggest_toolkit, read_toolkit]

    # Create temporary absorption session
    absorption_session = await SessionModel.objects.acreate(
        user=session.user,
        engine_type=engine.engine_type,
        transport_type="api",
        status="active",
        metadata={"absorption_source": str(session.id)},
    )

    # Run the absorption agent — drain all responses
    async for _response in run_conversation_turn(
        engine,
        absorption_session,
        message,
        extra_tools=toolkits,
    ):
        pass  # Toolkit tools are handled by the runner internally

    # Mark absorption session as completed
    absorption_session.status = "completed"
    await absorption_session.asave(update_fields=["status"])

    return suggest_toolkit
