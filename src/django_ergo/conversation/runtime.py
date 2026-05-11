"""Reusable runtime helpers for one-shot generation and workflow-backed tasks.

This module is the shared layer other apps should depend on. It exposes a
small runtime API over the lower-level engine classes without importing any
app-specific model/provider policy.
"""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field
from typing import TYPE_CHECKING
from typing import Any

from django.utils.module_loading import import_string

from django_ergo.conversation.engines import ENGINE_REGISTRY
from django_ergo.conversation.runner import PendingApproval
from django_ergo.conversation.runner import run_conversation_turn
from django_ergo.settings import api_settings

if TYPE_CHECKING:
    from django_ergo.conversation.engine import Engine
    from django_ergo.conversation.engine import EngineResponse
    from django_ergo.conversation.models import ConversationSession
    from django_ergo.conversation.toolkit import Toolkit


@dataclass(frozen=True)
class EngineSpec:
    """Low-level engine selection.

    This is intentionally a runtime concern rather than a user-facing plugin
    surface. App code may override it; the default plugin path should usually
    rely on settings-backed defaults instead.
    """

    engine_type: str
    transport_type: str = "api"
    config: dict[str, Any] = field(default_factory=dict)


@dataclass
class AssistantTaskResult:
    """Collected result for a workflow-backed assistant turn."""

    session: ConversationSession
    events: list[EngineResponse]
    approvals: list[PendingApproval]
    text: str


def get_default_engine_spec() -> EngineSpec:
    """Return the settings-backed default engine selection."""
    return EngineSpec(
        engine_type=api_settings.CONVERSATION_ENGINE_TYPE,
        transport_type=api_settings.CONVERSATION_TRANSPORT_TYPE,
        config=dict(api_settings.CONVERSATION_ENGINE_CONFIG),
    )


def build_engine(spec: EngineSpec | None = None) -> Engine:
    """Instantiate an engine from a spec."""
    active_spec = spec or get_default_engine_spec()
    key = (active_spec.engine_type, active_spec.transport_type)
    engine_path = ENGINE_REGISTRY.get(key)
    if not engine_path:
        msg = f"No engine registered for {key}"
        raise ValueError(msg)
    engine_cls = import_string(engine_path)
    return engine_cls(config=active_spec.config)


async def generate_once(  # noqa: PLR0913
    prompt: str,
    *,
    engine: Engine | None = None,
    engine_spec: EngineSpec | None = None,
    workflow=None,
    system: str | None = None,
    response_model: type | None = None,
):
    """Run one tool-free generation using an explicit or default engine."""
    active_engine = engine or build_engine(engine_spec)
    return await active_engine.generate(
        prompt=prompt,
        workflow=workflow,
        system=system,
        response_model=response_model,
    )


async def run_workflow_task(  # noqa: PLR0913
    *,
    user,
    workflow,
    message: str,
    engine: Engine | None = None,
    engine_spec: EngineSpec | None = None,
    extra_tools: list[Toolkit] | None = None,
    metadata: dict[str, Any] | None = None,
    close_session: bool = True,
) -> AssistantTaskResult:
    """Create a conversation session, run one workflow-backed turn, and collect output."""
    from django_ergo.conversation.manager import SessionManager

    spec = engine_spec or get_default_engine_spec()
    session_manager = SessionManager()
    session_metadata = dict(spec.config)
    if metadata:
        session_metadata.update(metadata)
    session = await session_manager.create_session(
        user=user,
        workflow=workflow,
        engine_type=spec.engine_type,
        transport_type=spec.transport_type,
        metadata=session_metadata,
    )
    active_engine = engine or await session_manager.get_engine(session)

    text_parts: list[str] = []
    events = []
    approvals = []
    async for event in run_conversation_turn(
        active_engine,
        session,
        message,
        extra_tools=extra_tools,
    ):
        if isinstance(event, PendingApproval):
            approvals.append(event)
            continue
        events.append(event)
        if event.text:
            text_parts.append(event.text)

    if close_session:
        await session_manager.close_session(session)

    return AssistantTaskResult(
        session=session,
        events=events,
        approvals=approvals,
        text="".join(text_parts),
    )
