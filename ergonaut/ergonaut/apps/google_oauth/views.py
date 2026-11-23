"""Human-only pages to connect Google accounts to a private bot (docs/google-oauth.md).

Nothing here touches a model, a chat or a tool: the client secret comes from the server's
environment, the authorization code goes from Google's redirect straight into a server-side
exchange, and the callback answers with a redirect so no code stays in a page URL. Errors are
fixed reason codes; codes, tokens, secrets and Google's responses are never logged or shown."""

from __future__ import annotations

import logging
from datetime import date, datetime
from functools import wraps

from django.http import Http404, HttpResponse, HttpResponseRedirect
from django.shortcuts import render
from django.urls import reverse
from django.utils.http import urlencode
from django.views.decorators.cache import never_cache
from django.views.decorators.debug import sensitive_variables
from django.views.decorators.http import require_GET, require_POST

from ergonaut.apps.google_oauth import connectors, flow

logger = logging.getLogger(__name__)

ACCOUNT_FIELDS = (
    "email",
    "label",
    "status",
    "gmail_status",
    "gmail_last_sync",
    "calendar_status",
    "calendar_last_sync",
)


def human_only(view):
    """Require a signed-in user and prevent caching and referrer leakage."""

    @wraps(view)
    def wrapped(request, *args, **kwargs):
        if not request.user.is_authenticated:
            if request.path == reverse("google-oauth-callback"):
                response = HttpResponse("Sign-in session expired. Start again.", status=403)
                response["Referrer-Policy"] = "no-referrer"
                response["Cache-Control"] = "no-store"
                return response
            return HttpResponseRedirect("/login?" + urlencode({"next": request.get_full_path()}))
        response = view(request, *args, **kwargs)
        response.headers["Referrer-Policy"] = "no-referrer"
        response["Cache-Control"] = "no-store"
        return response

    return wrapped


def _ctx(request, connector):
    from django_ergo.bots.tools import ToolContext

    return ToolContext(bot=connector.bot, user=request.user)


def _connector(request, bot_name):
    try:
        return connectors.load(bot_name, request.user)
    except connectors.ConnectorError as exc:
        # An unauthorised user and an unknown bot look the same.
        raise Http404(str(exc)) from None


def _client(request, connector):
    """(client_id, client_secret) from the environment, or None when either is missing."""
    ctx = _ctx(request, connector)
    client_id, secret = ctx.secret(connector.client_id_env), ctx.secret(connector.client_secret_env)
    return (client_id, secret) if client_id and secret else None


def _cell(value) -> str:
    if isinstance(value, datetime | date):
        return value.isoformat(timespec="minutes") if isinstance(value, datetime) else value.isoformat()
    return str(value)[:80] if value not in (None, "") else ""


def _accounts(request, connector) -> list[dict]:
    try:
        rows = connector.hook("google_list_accounts")(_ctx(request, connector)) or []
    except Exception:  # noqa: BLE001 - a bot error isn't shown
        logger.warning("google-oauth %s: listing accounts failed", connector.bot_name)
        return []
    return [{k: _cell(row.get(k)) for k in ACCOUNT_FIELDS} | {"scopes": _scopes(row.get("scopes"))} for row in rows]


def _scopes(value) -> list[str]:
    items = value.split() if isinstance(value, str) else list(value or [])
    return [str(s).removeprefix(connectors.SCOPE_PREFIX)[:60] for s in items if s]


WRITE_SCOPES = tuple(connectors.SCOPE_PREFIX + n for n in ("calendar.events", "gmail.modify"))
DISCLOSURES = {
    "gmail.readonly": "Read your email (the bot stores headers and short snippets).",
    "gmail.compose": "Create drafts. This permission technically allows sending mail, but the bot has no send route.",
    "gmail.modify": "Read and change your email, labels and drafts.",
    "calendar.readonly": "Read your calendars and events.",
    "calendar.events.readonly": "Read your calendar events.",
    "calendar.events": "Create, change and delete calendar events, including adding guests: Google emails the invitations when the bot asks it to.",
    "calendar.calendarlist.readonly": "List which calendars you have.",
}


def _disclosures(connector) -> list[dict]:
    return [{"scope": s, "text": DISCLOSURES.get(s, "")} for s in _scopes(connector.scopes)]


def _back(connector) -> str:
    return reverse("google-oauth-page") + "?" + urlencode({"bot": connector.bot_name})


def _flash(request, code: str) -> None:
    request.session[flow.FLASH_KEY] = code


@never_cache
@require_GET
@human_only
def page(request):
    connector = _connector(request, request.GET.get("bot", ""))
    code = request.session.pop(flow.FLASH_KEY, "")
    configured = _client(request, connector) is not None
    uri = flow.redirect_uri()
    return render(
        request,
        "google_oauth/page.html",
        {
            "connector": connector,
            "scopes": _disclosures(connector),
            "writes": any(s in connector.scopes for s in WRITE_SCOPES),
            "accounts": _accounts(request, connector),
            "configured": configured,
            "client_id_env": connector.client_id_env,
            "client_secret_env": connector.client_secret_env,
            "redirect_uri": uri,
            "ready": configured and bool(uri),
            "message": flow.REASONS.get(code, ""),
            "message_ok": code in ("connected", "disconnected"),
        },
    )


@never_cache
@require_POST
@human_only
def start(request):
    connector = _connector(request, request.POST.get("bot", ""))
    client = _client(request, connector)
    if client is None or not flow.redirect_uri():
        _flash(request, "not_configured")
        return HttpResponseRedirect(_back(connector))
    hint = flow.check_login_hint(request.POST.get("login_hint", ""))
    hd = flow.check_hd(request.POST.get("hd", ""))
    if hint is None or hd is None:
        _flash(request, "account")
        return HttpResponseRedirect(_back(connector))
    return HttpResponseRedirect(flow.begin(request, connector, client[0], login_hint=hint, hd=hd))


@sensitive_variables("code", "client", "account")
@never_cache
@require_GET
@human_only
def callback(request):
    """Google's redirect. The code is exchanged here and the answer is a redirect to a clean page."""
    code, state = request.GET.get("code", ""), request.GET.get("state", "")
    entry = flow.take(request, state)  # one use: gone whatever happens next
    if entry is None:
        return HttpResponse(flow.REASONS["state"], status=400, content_type="text/plain")
    try:
        connector = connectors.load(entry["bot"], request.user)
    except connectors.ConnectorError:
        raise Http404 from None
    reason = _finish(request, connector, entry, code)
    logger.info("google-oauth %s: %s", connector.bot_name, reason)
    _flash(request, reason)
    response = HttpResponseRedirect(_back(connector))
    response["Referrer-Policy"] = "no-referrer"
    return response


@sensitive_variables("code", "client", "account")
def _finish(request, connector, entry, code) -> str:
    if request.GET.get("error") or not code:
        return "denied"
    client = _client(request, connector)
    if client is None or not flow.redirect_uri():
        return "not_configured"
    account, reason = flow.exchange(entry, connector, client[0], client[1], code)
    if account is None:
        return reason
    account["owner_id"] = request.user.get_username()
    try:
        connector.hook("google_store_account")(_ctx(request, connector), account)
    except Exception:  # noqa: BLE001 - the error could carry the token; say only that it failed
        return "store"
    return "connected"


@sensitive_variables("token")
@never_cache
@require_POST
@human_only
def disconnect(request):
    connector = _connector(request, request.POST.get("bot", ""))
    email = request.POST.get("email", "")
    if email not in {a["email"] for a in _accounts(request, connector)}:
        _flash(request, "unknown_account")
        return HttpResponseRedirect(_back(connector))
    ctx = _ctx(request, connector)
    revoked = False
    try:
        token = connector.hook("google_revoke_token")(ctx, email)
        revoked = bool(token) and flow.revoke(token)
    except Exception:  # noqa: BLE001
        logger.warning("google-oauth %s: reading the token to revoke failed", connector.bot_name)
    try:
        connector.hook("google_delete_account")(ctx, email)
    except Exception:  # noqa: BLE001
        logger.warning("google-oauth %s: delete failed", connector.bot_name)
        _flash(request, "store")
        return HttpResponseRedirect(_back(connector))
    _flash(request, "disconnected" if revoked else "disconnected_local")
    return HttpResponseRedirect(_back(connector))
