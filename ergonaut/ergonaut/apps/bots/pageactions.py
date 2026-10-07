"""Recording page action calls, and showing the bot the latest ones.

``DjangoCallLog`` is the host side of ``django_ergo.bots.page_actions``: Ergonaut keeps
each call that ran as a ``PageActionCall`` and gives the bot, on its next turn, the
calls made from that chat since its last reply.
"""

from __future__ import annotations

import json

MAX_OUTCOME = 200


def outcome_text(call) -> str:
    """One line on how a call went: the error, the action's message, or its result."""
    result = call.result or {}
    if call.error:
        text = f"failed: {call.error}"
    elif result.get("message"):
        text = f"ok: {result['message']}"
    else:
        text = "ok" + (f": {json.dumps(result, default=str)}" if result else "")
    if call.approved:
        text += " (viewer approved it)"
    return text[:MAX_OUTCOME]


class DjangoCallLog:
    def since_last_reply(self, session, limit: int) -> list[dict]:
        from django_ergo.conversation.models import SessionMessageRole

        from ergonaut.apps.bots.models import PageActionCall

        last_reply = (
            session.messages.filter(role=SessionMessageRole.ASSISTANT)
            .order_by("-created_at")
            .values_list("created_at", flat=True)
            .first()
        )
        calls = PageActionCall.objects.filter(session=session).select_related("user")
        if last_reply is not None:
            calls = calls.filter(created_at__gt=last_reply)
        newest = list(calls.order_by("-created_at")[:limit])
        return [
            {"who": c.user.get_username(), "action": c.action, "args": c.args, "outcome": outcome_text(c)}
            for c in reversed(newest)
        ]
