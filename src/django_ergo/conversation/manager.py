"""Session manager — orchestrates engine lifecycle."""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.utils.module_loading import import_string

from django_ergo.conversation.engines import ENGINE_REGISTRY
from django_ergo.conversation.models import ConversationSession

if TYPE_CHECKING:
    from uuid import UUID

    from django_ergo.conversation.engine import Engine
    from django_ergo.models import Workflow


class SessionManager:
    def __init__(self):
        self._active_engines: dict[UUID, Engine] = {}

    async def create_session(  # noqa: PLR0913
        self,
        user,
        workflow: Workflow | None,
        engine_type: str,
        transport_type: str,
        metadata: dict | None = None,
    ) -> ConversationSession:
        session = await ConversationSession.objects.acreate(
            user=user,
            workflow=workflow,
            engine_type=engine_type,
            transport_type=transport_type,
            status="active",
            metadata=metadata or {},
        )
        engine = self._build_engine(session)
        session.session_id = await engine.start_session(session)
        await session.asave()
        self._active_engines[session.id] = engine
        return session

    async def get_engine(self, session: ConversationSession) -> Engine:
        if session.id in self._active_engines:
            return self._active_engines[session.id]
        engine = self._build_engine(session)
        await engine.resume_session(session)
        self._active_engines[session.id] = engine
        return engine

    async def close_session(self, session: ConversationSession) -> None:
        if engine := self._active_engines.pop(session.id, None):
            await engine.close_session(session)
        session.status = "completed"
        await session.asave()

    def _build_engine(self, session: ConversationSession) -> Engine:
        key = (session.engine_type, session.transport_type)
        engine_path = ENGINE_REGISTRY.get(key)
        if not engine_path:
            msg = f"No engine registered for {key}"
            raise ValueError(msg)
        engine_cls = import_string(engine_path)
        return engine_cls(config=session.metadata)
