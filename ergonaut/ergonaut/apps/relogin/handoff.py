"""Short-lived Redis keys between the web views and the worker that holds the login.

- ``relogin:<id>:show``: what the page shows (sign-in link, device code) and the recorded
  ``state``; lives no longer than the session, deleted when it ends.
- ``relogin:<id>:input``: the one pasted code or redirect URL; TTL 60 s, read-and-delete.
- ``relogin:<id>:cancel``: Lee pressed Cancel.

Values are never logged."""

from __future__ import annotations

import json

INPUT_TTL = 60


def client():
    from ergonaut.apps.bots.tasks import redis_client

    return redis_client()


def _key(session_id: str, name: str) -> str:
    return f"relogin:{session_id}:{name}"


def show(session_id: str, data: dict, ttl: int) -> None:
    client().set(_key(session_id, "show"), json.dumps(data), ex=max(1, int(ttl)))


def shown(session_id: str) -> dict | None:
    raw = client().get(_key(session_id, "show"))
    return json.loads(raw) if raw else None


def put_input(session_id: str, value: str) -> bool:
    """Queue the submission; False if one is already waiting."""
    return bool(client().set(_key(session_id, "input"), value, ex=INPUT_TTL, nx=True))


def take_input(session_id: str) -> str | None:
    raw = client().getdel(_key(session_id, "input"))
    return raw.decode() if isinstance(raw, bytes) else raw


def cancel(session_id: str) -> None:
    client().set(_key(session_id, "cancel"), "1", ex=3600)


def cancelled(session_id: str) -> bool:
    return bool(client().exists(_key(session_id, "cancel")))


def clear(session_id: str) -> None:
    client().delete(_key(session_id, "show"), _key(session_id, "input"))
