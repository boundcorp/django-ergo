"""Promote an existing thread to a named chat, and undo that promotion."""

from __future__ import annotations

from uuid import UUID

from django.db import transaction
from django.utils import timezone

from django_ergo.conversation import roles
from django_ergo.conversation.models import ConversationSession


def adopt_chat(  # noqa: C901, PLR0912, PLR0913, PLR0915
    bot,
    chat: str,
    session: ConversationSession,
    *,
    user=None,
    dry_run=False,
    rollback=False,
):
    """Adopt ``session`` for ``bot`` and ``chat``, or restore its previous role.

    Returns the session and whether a change was (or would be) made. A dry run
    performs the same checks but never writes. The caller must supply a loaded bot.
    """
    if not rollback and (chat in roles.MAIN_NAMES or chat not in bot.definition.chats):
        msg = f"{bot.name} has no named chat {chat!r}"
        raise ValueError(msg)
    with transaction.atomic():
        # Serialize competing adoptions for this user, even when no chat exists yet.
        type(session.user).objects.select_for_update().get(pk=session.user_id)
        current = ConversationSession.objects.select_for_update().get(pk=session.pk)
        if current.bot_name != bot.name:
            msg = "Session belongs to a different bot"
            raise ValueError(msg)
        if user is not None and current.user_id != user.pk:
            msg = "Session belongs to a different user"
            raise ValueError(msg)
        metadata = dict(current.metadata or {})
        original = metadata.get("adopted_from")
        if rollback:
            if not original:
                msg = "Session has not been adopted"
                raise ValueError(msg)
            if roles.role(current) != roles.CHAT or metadata.get("chat") != chat:
                msg = "Session is not the requested adopted chat"
                raise ValueError(msg)
            parent_id = original["parent_id"]
            if (
                parent_id
                and not ConversationSession.objects.filter(pk=parent_id).exists()
            ):
                msg = f"Original parent {parent_id} no longer exists"
                raise ValueError(msg)
            if not dry_run:
                for key in ("bot_role", "title", "chat"):
                    if key in original.get("present", []):
                        metadata[key] = original.get(key)
                    else:
                        metadata.pop(key, None)
                metadata.pop("adopted_from")
                current.metadata = metadata
                current.parent_id = UUID(parent_id) if parent_id else None
                current.status = original["status"]
                current.system_prompt = original["system_prompt"]
                current.save(
                    update_fields=[
                        "metadata",
                        "parent",
                        "status",
                        "system_prompt",
                        "updated_at",
                    ]
                )
            return current, True
        if roles.is_main(current):
            msg = "The main chat cannot be adopted"
            raise ValueError(msg)
        if (
            roles.role(current) == roles.CHAT
            and metadata.get("chat") == chat
            and original
        ):
            return current, False
        if roles.role(current) != roles.THREAD or original:
            msg = "Only an unadopted thread can be adopted"
            raise ValueError(msg)
        existing = (
            bot.sessions(current.user)
            .filter(metadata__bot_role=roles.CHAT, metadata__chat=chat)
            .exclude(status="completed")
            .exclude(pk=current.pk)
            .order_by("created_at")
            .first()
        )
        if existing:
            msg = f"Named chat {chat!r} already exists: {existing.pk}"
            raise ValueError(msg)
        if not dry_run:
            metadata["adopted_from"] = {
                "bot_role": metadata.get("bot_role"),
                "parent_id": str(current.parent_id) if current.parent_id else None,
                "title": metadata.get("title"),
                "chat": metadata.get("chat"),
                "status": current.status,
                "system_prompt": current.system_prompt,
                "present": [
                    key for key in ("bot_role", "title", "chat") if key in metadata
                ],
                "adopted_at": timezone.now().isoformat(),
            }
            metadata.update(bot_role=roles.CHAT, chat=chat)
            current.metadata = metadata
            current.parent = None
            if current.status == "completed":
                current.status = "active"
            if not metadata.get("custom_instructions"):
                current.system_prompt = bot.instructions_for(chat)
            current.save(
                update_fields=[
                    "metadata",
                    "parent",
                    "status",
                    "system_prompt",
                    "updated_at",
                ]
            )
        return current, True
