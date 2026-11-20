"""Staff-only pages for a self-service re-login (docs/relogin.md).

Starting, pasting and cancelling are POSTs behind CSRF. A session belongs to the staff
user who started it; anyone else gets 404. Submitted values are never logged or echoed,
and errors are our own words."""

from __future__ import annotations

import logging
from datetime import timedelta
from functools import wraps

from django.conf import settings
from django.db import IntegrityError, transaction
from django.http import Http404, HttpResponseRedirect, JsonResponse
from django.shortcuts import render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import urlencode
from django.views.decorators.cache import never_cache
from django.views.decorators.debug import sensitive_post_parameters, sensitive_variables
from django.views.decorators.http import require_GET, require_POST

from ergonaut.apps.relogin import handoff, parsing, runner, stores
from ergonaut.apps.relogin.models import ReloginSession

logger = logging.getLogger(__name__)

INSTRUCTIONS = {
    "device_code": "Open the sign-in page, sign in, and enter the code shown here. Nothing comes back to this page; it finishes by itself.",
    "paste_code": "Open the sign-in page and sign in. The page then shows a code: copy it and paste it below.",
    "paste_redirect": "Open the sign-in page and sign in. The browser then fails to load a localhost page: copy that whole address from the address bar and paste it below.",
}


def staff_only(view):
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return HttpResponseRedirect("/login?" + urlencode({"next": request.get_full_path()}))
        if not request.user.is_staff:
            raise Http404
        return view(request, *args, **kwargs)

    return wrapped


def _own(request, session_id: str) -> ReloginSession:
    session = ReloginSession.objects.filter(pk=session_id, user=request.user).first()
    if session is None:
        raise Http404
    return session


def _ready() -> str:
    """Why sessions can't run here, or "" when Redis and a Celery broker are set up."""
    if not getattr(settings, "CELERY_BROKER_URL", None) or handoff.client() is None:
        return "Re-login needs Redis and a Celery worker."
    return ""


@never_cache
@require_GET
@staff_only
def start_page(request):
    bot_name, key = request.GET.get("bot", ""), request.GET.get("store", "")
    context = {"bot_name": bot_name, "key": key, "error": "", "store": None, "active": None}
    try:
        store = stores.load(bot_name, key)
    except stores.StoreError as exc:
        context["error"] = str(exc)
        return render(request, "relogin/start.html", context, status=404)
    runner.sweep()
    context["store"] = store
    context["instructions"] = INSTRUCTIONS[store.shape]
    context["active"] = ReloginSession.objects.filter(bot_name=store.bot_name, store=store.key, outcome="").first()
    context["recent"] = ReloginSession.objects.filter(bot_name=store.bot_name, store=store.key)[:5]
    context["error"] = _ready()
    return render(request, "relogin/start.html", context)


@never_cache
@require_POST
@staff_only
def start(request):
    bot_name, key = request.POST.get("bot", ""), request.POST.get("store", "")
    try:
        store = stores.load(bot_name, key)
    except stores.StoreError as exc:
        return render(request, "relogin/start.html", {"error": str(exc), "store": None}, status=404)
    back = reverse("relogin-start-page") + "?" + urlencode({"bot": store.bot_name, "store": store.key})
    problem = _ready()
    if problem:
        return render(request, "relogin/start.html", {"error": problem, "store": store}, status=503)
    runner.sweep()
    try:
        with transaction.atomic():
            session = ReloginSession.objects.create(
                user=request.user,
                bot_name=store.bot_name,
                store=store.key,
                command=store.command,
                shape=store.shape,
                port_group=store.port_group,
                expires_at=timezone.now() + timedelta(seconds=store.max_seconds),
            )
    except IntegrityError:
        active = ReloginSession.objects.filter(
            bot_name=store.bot_name, store=store.key, outcome="", user=request.user
        ).first()
        if active:
            return HttpResponseRedirect(reverse("relogin-session", args=[active.pk]))
        return HttpResponseRedirect(back)  # another session holds the store or its port
    from ergonaut.apps.relogin.tasks import run_relogin

    transaction.on_commit(lambda: run_relogin.delay(session.pk))
    logger.info("relogin %s started for %s/%s", session.pk, store.bot_name, store.key)
    return HttpResponseRedirect(reverse("relogin-session", args=[session.pk]))


@never_cache
@require_GET
@staff_only
def session_page(request, session_id):
    session = _own(request, session_id)
    return render(
        request,
        "relogin/session.html",
        {
            "session": session,
            "instructions": INSTRUCTIONS.get(session.shape, ""),
            "again": reverse("relogin-start-page") + "?" + urlencode({"bot": session.bot_name, "store": session.store}),
        },
    )


@sensitive_variables("shown")
@never_cache
@require_GET
@staff_only
def status(request, session_id):
    session = _own(request, session_id)
    if session.active and session.expires_at < timezone.now() - timedelta(seconds=90):
        runner.sweep()
        session.refresh_from_db()
    data = {
        "state": session.state,
        "outcome": session.outcome,
        "shape": session.shape,
        "seconds_left": max(0, int((session.expires_at - timezone.now()).total_seconds())),
        "error": session.error,
        "command": session.command,
        "post_check": session.post_check,
        "account": session.account,
        "exit_code": session.exit_code,
        "submitted": session.submitted_at is not None,
    }
    if session.active and session.state == "waiting":
        shown = handoff.shown(session.pk) or {}
        data["url"] = shown.get("url")
        data["code"] = shown.get("code")
    return JsonResponse(data)


@sensitive_post_parameters("value")
@sensitive_variables("value", "checked", "shown")
@never_cache
@require_POST
@staff_only
def submit(request, session_id):
    session = _own(request, session_id)
    try:
        if not session.active or session.state != "waiting" or session.shape == "device_code":
            return JsonResponse({"error": "This session isn't waiting for input."}, status=409)
        value = request.POST.get("value", "")
        shown = handoff.shown(session.pk) or {}
        if session.shape == "paste_code":
            checked = parsing.check_paste_code(value, state=shown.get("state"))
        else:
            store = stores.load(session.bot_name, session.store)
            checked = parsing.check_redirect(value, port=store.port, state=shown.get("state"))
        # Single use: claim the one submission in the row, then hand it to the worker.
        claimed = ReloginSession.objects.filter(
            pk=session.pk, outcome="", state="waiting", submitted_at__isnull=True
        ).update(submitted_at=timezone.now())
        if not claimed or not handoff.put_input(session.pk, checked):
            return JsonResponse({"error": "A submission was already made for this session."}, status=409)
    except parsing.InputError as exc:
        return JsonResponse({"error": str(exc)}, status=400)
    except stores.StoreError as exc:
        return JsonResponse({"error": str(exc)}, status=409)
    except Exception as exc:  # noqa: BLE001 - never let a traceback carry the pasted value
        logger.warning("relogin %s: submit failed (%s)", session_id, type(exc).__name__)
        return JsonResponse({"error": "The submission could not be handed over."}, status=500)
    logger.info("relogin %s: submission accepted", session.pk)
    return JsonResponse({"ok": True})


@never_cache
@require_POST
@staff_only
def cancel(request, session_id):
    session = _own(request, session_id)
    if session.active:
        ReloginSession.objects.filter(pk=session.pk, outcome="").update(cancel_requested_at=timezone.now())
        handoff.cancel(session.pk)
        if session.state == "starting" and session.created_at < timezone.now() - timedelta(seconds=60):
            runner.finish(session, "cancelled", error="Cancelled before the worker started.", only_state="starting")
        logger.info("relogin %s: cancel requested", session.pk)
    return JsonResponse({"ok": True})
