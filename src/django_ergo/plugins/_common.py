"""Helpers the official plugins share. Grep here before writing another."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from datetime import datetime

MAX_OUTPUT_CHARS = 20_000
TOKEN_FIELDS = ("input", "cache_write", "cache_read", "output", "reasoning")


def trim(text: str, limit: int = MAX_OUTPUT_CHARS) -> str:
    """Keep the start and the end of long output, where the useful parts are."""
    if len(text) <= limit:
        return text
    head = text[: limit // 4]
    tail = text[-(limit - len(head)) :]
    skipped = len(text) - len(head) - len(tail)
    return f"{head}\n[... {skipped} characters skipped ...]\n{tail}"


def clip(text: str, limit: int, *, one_line: bool = True) -> str:
    """``text`` cut to ``limit`` characters with an ellipsis; ``one_line``
    first collapses its whitespace."""
    text = " ".join(str(text).split()) if one_line else str(text).strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def is_secret(name: str) -> bool:
    """A file name that looks like a key or credentials, never shown or sent."""
    name = name.lower()
    return (
        name.startswith((".env", "id_rsa", "id_ed25519"))
        or name.endswith((".pem", ".key", ".p12", ".pfx"))
        or "secret" in name
        or "credential" in name
    )


def record_agent_usage(  # noqa: PLR0913
    worker,
    *,
    session,
    source: str,
    agent: str,
    models: dict[str, dict],
    first_at: datetime | None = None,
    last_at: datetime | None = None,
    replace: bool = False,
) -> None:
    """Store a coding agent's token totals per model on ``worker``.

    ``models`` maps a model to its totals (``input``, ``cache_write``,
    ``cache_read``, ``output``, ``reasoning``, ``requests``, and optionally its
    own ``first_at``/``last_at``). ``replace`` drops the worker's rows for
    models no longer listed.
    """
    from django_ergo.conversation.models import AgentUsage

    for model, usage in models.items():
        AgentUsage.objects.update_or_create(
            worker=worker,
            model=model,
            defaults={
                "session": session,
                "bot_name": worker.bot_name,
                "source": source,
                "agent": agent,
                **{
                    f"{field}_tokens": int(usage.get(field) or 0)
                    for field in TOKEN_FIELDS
                },
                "requests": int(usage.get("requests") or 0),
                "first_at": usage.get("first_at", first_at),
                "last_at": usage.get("last_at", last_at),
            },
        )
    if replace:
        AgentUsage.objects.filter(worker=worker).exclude(
            model__in=list(models)
        ).delete()
