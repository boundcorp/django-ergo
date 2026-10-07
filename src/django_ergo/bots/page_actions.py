"""Page actions: functions a ``.jhtml`` page calls as the viewer.

A bot declares them next to its tools with ``@page_action`` (see
``django_ergo.bots.tools``). A page calls one through the bridge script
``render_page`` puts in every page::

    const result = await ergo.call("restock", {item: "flour", qty: 2})

The page never makes the request: the app that shows it (Ergonaut's page
viewer) does, with the viewer's own login, for the bot and chat the page was
opened from. ``call_page_action`` is what the host runs for that request. It
checks the arguments against the schema inferred from the function, asks for
approval first when the action ``requires_approval``, and runs the function
with a ``ToolContext`` whose ``user`` is the viewer.

Approval is a round trip: the first call answers ``needs_approval`` with a
preview and a token (``django.core.signing`` over the user, bot, action and a
hash of the arguments, valid ``APPROVAL_MAX_AGE`` seconds); the viewer shows
the preview and, on Yes, repeats the call with the token. The token goes to
the viewer, never to the page, and works only for those exact arguments.

A host can keep a log of calls (``set_call_log``): the bot then sees the
latest ones on its next turn, in the context section ``CONTEXT_TITLE``.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import logging
from dataclasses import dataclass
from dataclasses import field
from typing import TYPE_CHECKING
from typing import Any
from typing import Protocol

if TYPE_CHECKING:
    from django_ergo.bots.runtime import Bot
    from django_ergo.bots.tools import PageAction
    from django_ergo.bots.tools import ToolContext

logger = logging.getLogger(__name__)

# What a host gives a page action to finish in; longer work belongs in ``ctx.tasks``.
TIMEOUT_SECONDS = 30
APPROVAL_MAX_AGE = 5 * 60
APPROVAL_SALT = "django_ergo.page_action_approval"
CONTEXT_TITLE = "Page actions since your last reply"
CONTEXT_LIMIT = 20

_JSON_TYPES: dict[str, tuple[type, ...]] = {
    "string": (str,),
    "integer": (int,),
    "number": (int, float),
    "boolean": (bool,),
    "array": (list,),
    "object": (dict,),
}


class PageActionError(Exception):
    """A call a host should answer with ``status`` (a 4xx or 5xx) and ``message``.

    ``ran`` says the action's function ran (and failed), as opposed to the call being
    refused first (unknown action, bad arguments, approval).
    """

    def __init__(self, status: int, message: str, *, ran: bool = False):
        super().__init__(message)
        self.status = status
        self.message = message
        self.ran = ran


@dataclass
class PageActionOutcome:
    """What a call came to: a result, or a request for the viewer's approval first."""

    action: PageAction
    args: dict
    result: dict = field(default_factory=dict)
    needs_approval: bool = False
    preview: str = ""
    approval: str = ""  # the token, when ``needs_approval``
    approved: bool = False  # the action required approval and the viewer gave it


def validate_args(action: PageAction, args: Any) -> dict:
    """``args`` against the action's schema: missing required, unknown, or wrong JSON type."""
    if args is None:
        args = {}
    if not isinstance(args, dict):
        msg = "args must be an object"
        raise PageActionError(400, msg)
    missing = [name for name in action.required if name not in args]
    if missing:
        msg = f"{action.name}: missing {', '.join(missing)}"
        raise PageActionError(400, msg)
    unknown = [name for name in args if name not in action.parameters]
    if unknown:
        msg = f"{action.name}: unknown argument(s) {', '.join(sorted(unknown))}"
        raise PageActionError(400, msg)
    for name, value in args.items():
        kind = (action.parameters[name] or {}).get("type")
        allowed = _JSON_TYPES.get(kind) if isinstance(kind, str) else None
        if allowed is None:
            continue
        # bool is an int in Python; JSON keeps them apart.
        wrong = not isinstance(value, allowed) or (
            isinstance(value, bool) and kind != "boolean"
        )
        if wrong:
            msg = f"{action.name}: {name} must be {kind}"
            raise PageActionError(400, msg)
    return args


def args_hash(args: dict) -> str:
    return hashlib.sha256(
        json.dumps(args, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def approval_token(user, bot_name: str, action_name: str, args: dict) -> str:
    from django.core import signing

    return signing.dumps(
        {"u": str(user.pk), "b": bot_name, "a": action_name, "h": args_hash(args)},
        salt=APPROVAL_SALT,
    )


def check_approval(token: str, user, bot_name: str, action_name: str, args: dict):
    """Raise PageActionError unless ``token`` was issued to ``user`` for exactly this call."""
    from django.core import signing

    try:
        data = signing.loads(token, salt=APPROVAL_SALT, max_age=APPROVAL_MAX_AGE)
    except signing.SignatureExpired as exc:
        msg = "The approval expired; try again"
        raise PageActionError(400, msg) from exc
    except signing.BadSignature as exc:
        msg = "That approval isn't valid"
        raise PageActionError(403, msg) from exc
    expected = {
        "u": str(user.pk),
        "b": bot_name,
        "a": action_name,
        "h": args_hash(args),
    }
    if data != expected:
        msg = "That approval is for a different call"
        raise PageActionError(403, msg)


def approval_preview(action: PageAction, ctx: ToolContext, args: dict) -> str:
    """The text the viewer confirms: ``approval_preview(ctx, **args)``, with defaults filled in."""
    if action.approval_preview is None:
        return f"Run {action.name}" + (
            f" with {json.dumps(args, ensure_ascii=False)}" if args else ""
        )
    full = {
        name: param.default
        for name, param in list(inspect.signature(action.function).parameters.items())[
            1:
        ]
        if param.default is not inspect.Parameter.empty
    }
    full.update(args)
    preview = action.approval_preview(ctx, **full)
    return str(getattr(preview, "text", preview))


def _plain_result(value: Any) -> dict:
    if value is None:
        return {}
    if not isinstance(value, dict):
        value = {"value": value}
    # Must be JSON, or the viewer couldn't pass it to the page.
    return json.loads(json.dumps(value))


def call_page_action(  # noqa: PLR0913
    bot: Bot,
    name: str,
    args: Any = None,
    *,
    user,
    session=None,
    page: str | None = None,
    approval: str = "",
) -> PageActionOutcome:
    """Run the bot's page action ``name`` as ``user``; see the module docstring."""
    from django.core.exceptions import ValidationError

    from django_ergo.bots.tools import ToolContext

    action = bot.page_actions.get(name)
    if action is None:
        msg = f"No page action {name!r}"
        raise PageActionError(404, msg)
    args = validate_args(action, args)
    ctx = ToolContext(bot=bot, session=session, user=user, page=page)
    approved = False
    if action.requires_approval:
        if not approval:
            try:
                preview = approval_preview(action, ctx, args)
            except Exception:
                logger.exception("Page action %s: approval preview failed", name)
                msg = "Couldn't prepare the approval"
                raise PageActionError(500, msg) from None
            return PageActionOutcome(
                action,
                args,
                needs_approval=True,
                preview=preview,
                approval=approval_token(user, bot.name, name, args),
            )
        check_approval(approval, user, bot.name, name, args)
        approved = True
    try:
        raw = action.function(ctx, **args)
    except PageActionError as exc:
        exc.ran = True
        raise
    except ValidationError as exc:
        raise PageActionError(400, "; ".join(exc.messages), ran=True) from exc
    except ValueError as exc:
        raise PageActionError(400, str(exc), ran=True) from exc
    except Exception:
        logger.exception("Page action %s of %s failed", name, bot.name)
        msg = "The action failed"
        raise PageActionError(500, msg, ran=True) from None
    try:
        result = _plain_result(raw)
    except (TypeError, ValueError):
        logger.exception("Page action %s of %s returned non-JSON", name, bot.name)
        msg = "The action failed"
        raise PageActionError(500, msg, ran=True) from None
    return PageActionOutcome(action, args, result=result, approved=approved)


# -- the call log and the bot's context --------------------------------------------------


class CallLog(Protocol):
    """Where a host keeps page action calls (Ergonaut's ``PageActionCall`` table)."""

    def since_last_reply(self, session, limit: int) -> list[dict]:
        """Calls made from this chat since the bot's last reply, oldest first, newest ``limit``.

        Each is ``{"who": str, "action": str, "args": dict, "outcome": str}``.
        """
        ...


_call_log: CallLog | None = None


def set_call_log(log: CallLog | None) -> None:
    global _call_log  # noqa: PLW0603
    _call_log = log


def get_call_log() -> CallLog | None:
    return _call_log


def context_text(session) -> str:
    """The bot's "Page actions since your last reply" section (empty when there are none)."""
    log = _call_log
    if log is None or session is None or session.pk is None:
        return ""
    rows = log.since_last_reply(session, CONTEXT_LIMIT)
    return "\n".join(
        f"- {row['who']} ran {row['action']}"
        f"({json.dumps(row['args'], ensure_ascii=False, default=str)}): {row['outcome']}"
        for row in rows
    )
