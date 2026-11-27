"""Acceptance tests copied from the independent PR #416 review at 56fd9c1.

Mock upstreams only: every transport is an httpx.MockTransport or a local
fake process. No real provider key is read; fake strings only. Tests marked
FINDING demonstrate behaviour the review flags; the others confirm a claim.
"""

import json
import os
import subprocess
import sys
import time
from contextlib import suppress
from decimal import Decimal

import httpx
import pytest

from django_ergo.conversation import spend_guard
from django_ergo.conversation.models import InferenceRequest
from django_ergo.conversation.models import PaidAgentLaunch
from django_ergo.costs.ledger import summarize_costs
from django_ergo.costs.models import CostObservation
from tests.test_paid_agent_proxy import MODEL
from tests.test_paid_agent_proxy import Chunks
from tests.test_paid_agent_proxy import attempt
from tests.test_paid_agent_proxy import fixture
from tests.test_paid_agent_proxy import held as held_fixture
from tests.test_paid_agent_proxy import payload
from tests.test_paid_agent_proxy import receipt

held = held_fixture


def kinds(pk):
    return set(
        CostObservation.objects.filter(
            operation__operation_key=f"inference-request:{pk}"
        ).values_list("kind", flat=True)
    )


def sse(*events, done=False):
    out = b"".join(b"data: " + json.dumps(e).encode() + b"\n\n" for e in events)
    return out + (b"data: [DONE]\n\n" if done else b"")


def completed(**usage):
    result = receipt()
    result["usage"].update(usage)
    return {"type": "response.completed", "response": result}


# --- 1. ledger ingestion ----------------------------------------------------


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    "mode",
    [
        "json_ok",
        "stream_ok",
        "stream_cut",
        "stream_no_usage",
        "http_500",
        "missing_usage",
        "transport_error",
        "malformed_json",
    ],
)
def test_every_attempt_kind_reaches_ledger_even_with_legacy_flag_off(  # noqa: C901 - mode matrix
    held, settings, mode
):
    """Confirms claim 1: each attempt row is recorded; no usage is never $0."""
    settings.DJANGO_ERGO = {"INFERENCE_COST_LEDGER_ENABLED": False}

    def handler(request):  # noqa: PLR0911 - one branch per mode
        if mode == "json_ok":
            return httpx.Response(200, json=receipt())
        if mode == "stream_ok":
            return httpx.Response(200, stream=Chunks(sse(completed())))
        if mode == "stream_cut":
            return httpx.Response(200, stream=Chunks(sse(completed()), cut=True))
        if mode == "stream_no_usage":
            return httpx.Response(
                200, stream=Chunks(sse({"type": "response.output_text.delta"}))
            )
        if mode == "http_500":
            return httpx.Response(500, json={"error": {"message": "boom"}})
        if mode == "missing_usage":
            return httpx.Response(200, json={"model": MODEL, "status": "completed"})
        if mode == "malformed_json":
            return httpx.Response(200, content=b"{not json")
        msg = "mock refused"
        raise httpx.ConnectError(msg)

    proxy, launch, token, sent = fixture(held, handler)
    stream = mode.startswith("stream")
    try:
        response = proxy.request(token, "responses", payload(stream=stream))
        if stream:
            b"".join(response.streaming_content)
    except (httpx.HTTPError, ValueError):
        pass
    row = attempt()
    assert len(sent) == 1
    assert row.cost_ledger_state == "recorded"
    expected = "estimate" if mode in {"json_ok", "stream_ok"} else "unknown"
    assert kinds(row.pk) == {"reservation", expected}
    summary = {b["kind"]: b for b in summarize_costs(held.grantee)}
    if expected == "unknown":
        assert row.cost_usd is None and row.cost_status == "unknown"
        assert summary["unknown"]["unknown_count"] >= 1
        assert spend_guard.spent_and_reserved(held.pk)[1] == row.reserved_usd
    # The launch allocation is informational: reservation + release.
    assert kinds(launch.reservation_id) == {"reservation", "release"}


@pytest.mark.django_db(transaction=True)
def test_retries_and_parallel_attempts_each_recorded(held):
    proxy, launch, token, sent = fixture(held, max_attempts=3)
    for _ in range(3):
        proxy.request(token, "responses", payload())
    rows = list(launch.requests.order_by("attempt"))
    assert [r.attempt for r in rows] == [1, 2, 3]
    assert all(r.cost_ledger_state == "recorded" for r in rows)
    assert all(kinds(r.pk) == {"reservation", "estimate"} for r in rows)
    estimate = next(b for b in summarize_costs(held.grantee) if b["kind"] == "estimate")
    assert estimate["known_amount"] == sum(r.cost_usd for r in rows)


@pytest.mark.django_db(transaction=True)
def test_ledger_failure_is_retried_from_durable_outbox(held, monkeypatch):
    """Transient errors preserve charged source exposure and a retryable marker."""
    from django.core.management import call_command

    from django_ergo.costs import adapters

    real = adapters.record_inference_request
    outage = {"on": False}

    def flaky(**kwargs):
        if outage["on"]:
            msg = "transient ledger outage"
            raise RuntimeError(msg)
        return real(**kwargs)

    monkeypatch.setattr(adapters, "record_inference_request", flaky)
    proxy, launch, token, sent = fixture(held)
    outage["on"] = True  # outage only while this one attempt is in flight
    proxy.request(token, "responses", payload())
    outage["on"] = False
    row = attempt()
    assert row.cost_status == "priced"
    assert (
        row.cost_ledger_state == "pending"
    )  # durable work for the next maintenance sweep
    assert kinds(row.pk) == set()
    estimates = [b for b in summarize_costs(held.grantee) if b["kind"] == "estimate"]
    assert not estimates  # the paid spend is missing from reported totals
    # The spend cap still sees it (source rows), so this is a reporting gap.
    assert spend_guard.spent_and_reserved(held.pk)[0] == row.cost_usd
    from django_ergo.conversation.inference_ledger import retry_pending_costs
    from django_ergo.costs.coverage import coverage_manifest

    assert coverage_manifest(owner=held.grantee)["pending_inference_attempts"] == 1
    assert retry_pending_costs() == (1, 0)
    assert retry_pending_costs() == (0, 0)
    call_command("reconcile_inference_costs")
    assert coverage_manifest(owner=held.grantee)["pending_inference_attempts"] == 0
    assert (
        CostObservation.objects.filter(
            operation__operation_key=f"inference-request:{row.pk}", kind="estimate"
        ).count()
        == 1
    )
    assert kinds(row.pk) == {"reservation", "estimate"}


@pytest.mark.django_db(transaction=True)
def test_zero_reported_usage_retains_hold(held):
    """Implausible zero usage cannot forgive a nonempty request."""
    proxy, launch, token, sent = fixture(
        held,
        lambda req: httpx.Response(
            200,
            json={
                **receipt(),
                "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
            },
        ),
    )
    proxy.request(token, "responses", payload())
    row = attempt()
    assert row.cost_status == "unknown" and row.cost_usd is None
    assert spend_guard.spent_and_reserved(held.pk) == (Decimal(0), row.reserved_usd)


# --- 2. admission -----------------------------------------------------------


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("responses", payload(service_tier="priority")),
        ("responses", payload(previous_response_id="resp_x")),
        ("responses", payload(conversation="conv_x")),
        ("responses", payload(background=True)),
        ("responses", payload(prompt={"id": "pmpt_x"})),
        ("responses", payload(model=MODEL.upper())),
        ("responses", payload(model=MODEL + "-pro")),
        ("responses", payload(truncation="auto")),
        ("responses", payload(max_output_tokens=True)),
        ("responses", payload(include=["file_search_call.results"])),
        ("responses", payload(tools=[{"type": "web_search"}])),
        (
            "responses",
            payload(
                tools=[{"type": "namespace", "tools": [{"type": "code_interpreter"}]}]
            ),
        ),
        (
            "responses",
            payload(
                input=[
                    {
                        "role": "user",
                        "content": [{"type": "input_image", "image_url": "x"}],
                    }
                ]
            ),
        ),
        (
            "responses",
            payload(
                input=[
                    {
                        "type": "function_call_output",
                        "call_id": "c",
                        "output": [{"type": "input_file", "file_id": "f"}],
                    }
                ]
            ),
        ),
        ("responses", payload(input=[{"type": "item_reference", "id": "msg_x"}])),
        (
            "responses",
            payload(input=[{"type": "reasoning", "encrypted_content": "opaque"}]),
        ),
        ("chat/completions", {"model": MODEL, "messages": [], "n": 4}),
        ("chat/completions", {"model": MODEL, "messages": [], "service_tier": "flex"}),
        ("chat/completions", {"model": MODEL, "messages": [], "modalities": ["audio"]}),
        (
            "chat/completions",
            {"model": MODEL, "messages": [], "web_search_options": {}},
        ),
        (
            "chat/completions",
            {"model": MODEL, "messages": [{"role": "user", "audio": {"id": "a"}}]},
        ),
        ("chat/completions", {"model": MODEL, "messages": [], "prediction": {}}),
        ("embeddings", {"model": MODEL, "input": "x"}),
        ("responses", payload(input="x" * 2_100_000)),
    ],
)
def test_adversarial_bodies_refused_before_any_upstream_byte(held, path, body):
    proxy, launch, token, sent = fixture(held)
    with pytest.raises((spend_guard.SpendDenied, spend_guard.UnsupportedRequest)):
        proxy.request(token, path, json.loads(json.dumps(body)))
    assert sent == []
    assert not launch.requests.exists()


@pytest.mark.django_db(transaction=True)
def test_reservation_covers_worst_case_and_ceiling_is_injected(held):
    proxy, launch, token, sent = fixture(held)
    body = payload()
    body.pop("max_output_tokens")
    proxy.request(token, "responses", body)
    outgoing = json.loads(sent[0].content)
    row = attempt()
    guard = proxy.resolver(launch)[0]
    assert 0 < outgoing["max_output_tokens"] <= guard.max_output_tokens
    size = len(json.dumps(outgoing, ensure_ascii=True).encode())
    floor = guard.price.worst_case(
        size, outgoing["max_output_tokens"], cache_write=False
    )
    assert row.reserved_usd >= floor
    assert outgoing["store"] is False


@pytest.mark.django_db(transaction=True)
def test_hosted_tool_choice_is_refused(held):
    """Hosted tool selectors refuse before transport."""
    proxy, launch, token, sent = fixture(held)
    with pytest.raises(spend_guard.SpendDenied):
        proxy.request(
            token, "responses", payload(tool_choice={"type": "web_search_preview"})
        )
    assert len(sent) == 0


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("stream", [False, True])
def test_incomplete_max_output_settles_authoritative_usage(held, stream):
    """Authoritative truncated output is legitimate usage at bounded ceilings."""
    result = {
        **receipt(),
        "status": "incomplete",
        "incomplete_details": {"reason": "max_output_tokens"},
    }
    result["usage"].update(output_tokens=128, total_tokens=138)
    if stream:
        handler = lambda req: httpx.Response(  # noqa: E731
            200,
            stream=Chunks(sse({"type": "response.incomplete", "response": result})),
        )
    else:
        handler = lambda req: httpx.Response(200, json=result)  # noqa: E731
    proxy, launch, token, sent = fixture(held, handler)
    response = proxy.request(
        token, "responses", payload(stream=stream, max_output_tokens=128)
    )
    if stream:
        b"".join(response.streaming_content)
    row = attempt()
    assert row.cost_status == "priced" and row.cost_usd > 0
    assert row.prompt_tokens == 10 and row.output_tokens == 128
    assert spend_guard.spent_and_reserved(held.pk)[1] == 0
    launch.refresh_from_db()
    assert launch.revoked_at is None


@pytest.mark.django_db(transaction=True)
def test_actual_model_upgrade_never_priced_at_requested_tariff(held):
    proxy, launch, token, sent = fixture(
        held, lambda req: httpx.Response(200, json=receipt(MODEL + "-pro"))
    )
    proxy.request(token, "responses", payload())
    row = attempt()
    assert row.actual_model == MODEL + "-pro" and row.cost_status == "unknown"
    launch.refresh_from_db()
    assert launch.revoked_at is not None


@pytest.mark.django_db(transaction=True)
def test_overrun_beyond_reservation_is_never_forgiven(held):
    proxy, launch, token, sent = fixture(
        held,
        lambda req: httpx.Response(
            200,
            json={
                **receipt(),
                "usage": {
                    "input_tokens": 10_000_000,
                    "output_tokens": 5,
                    "total_tokens": 10_000_005,
                },
            },
        ),
    )
    proxy.request(token, "responses", payload())
    row = attempt()
    assert row.cost_status == "unknown"
    assert spend_guard.spent_and_reserved(held.pk)[1] == row.reserved_usd


# --- 3. caps, holds ---------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_cheap_settlements_never_replenish_launch_cap(held):
    proxy, launch, token, sent = fixture(held)
    proxy.request(token, "responses", payload())
    amount = attempt().reserved_usd
    PaidAgentLaunch.objects.filter(pk=launch.pk).update(cap_usd=amount * 2)
    proxy.request(token, "responses", payload())
    with pytest.raises(spend_guard.SpendDenied):
        proxy.request(token, "responses", payload())
    assert len(sent) == 2
    launch.refresh_from_db()
    assert launch.total_reserved_usd == amount * 2 and launch.revoked_at


@pytest.mark.django_db(transaction=True)
def test_revocation_does_not_release_in_flight_hold(held):
    from django.core.management import call_command

    proxy, launch, token, sent = fixture(held)
    _, pk, *_ = proxy.begin(token, "responses", payload())
    from django_ergo.conversation.paid_agent_proxy import revoke

    revoke(launch.pk)  # worker exit / watcher
    call_command("expire_paid_agent_launches")  # not yet expired: untouched
    row = InferenceRequest.objects.get(pk=pk)
    assert row.outcome == "pending"
    assert spend_guard.spent_and_reserved(held.pk)[1] == row.reserved_usd
    with pytest.raises(spend_guard.SpendDenied):
        proxy.request(token, "responses", payload())


# --- 4. credentials ---------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_token_is_launch_scoped(held):
    proxy, launch, token, sent = fixture(held)
    other_proxy, other, other_token, _ = fixture(held)
    proxy.request(token, "responses", payload())
    from django_ergo.conversation.paid_agent_proxy import authenticate
    from django_ergo.conversation.paid_agent_proxy import revoke

    assert authenticate(token).pk == launch.pk != other.pk
    revoke(launch.pk)
    with pytest.raises(spend_guard.SpendDenied):
        proxy.request(token, "responses", payload())
    other_proxy.request(other_token, "responses", payload())  # unaffected
    assert InferenceRequest.objects.filter(paid_launch=other).count() == 1
    assert launch.token_digest not in json.dumps(launch.authority)


def test_finding_mock_backend_is_unverified_same_uid_can_read_parent_secrets(
    tmp_path,
):
    """FINDING P1 (live blocker, documented): the sandbox contract is a dotted
    setting that nothing verifies. Under a prefix-only backend (the test
    double), a child that receives a capability-only env can still read a
    same-uid process's environment from /proc. Here a sentinel process holds a
    FAKE key the way Ergonaut's pod holds OPENAI_API_KEY."""
    fake_key = "sk-fake-review-sentinel-0000000000"
    sentinel = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        env={"PATH": os.environ.get("PATH", ""), "OPENAI_API_KEY": fake_key},
    )
    try:
        time.sleep(0.2)
        probe = (
            "import os,sys\n"
            "pid=sys.argv[1]\n"
            "data=open(f'/proc/{pid}/environ','rb').read()\n"
            "print('LEAK' if b'OPENAI_API_KEY=' in data else 'SAFE')\n"
        )
        child_env = {"PATH": os.environ.get("PATH", ""), "ERGO_PROXY_TOKEN": "cap"}
        out = subprocess.run(
            ["env", sys.executable, "-c", probe, str(sentinel.pid)],
            env=child_env,
            capture_output=True,
            text=True,
            check=True,
            cwd=tmp_path,
        ).stdout.strip()
        assert out == "LEAK"
        assert "OPENAI_API_KEY" not in child_env
    finally:
        sentinel.kill()
        sentinel.wait()


def test_ambient_provider_key_is_refused(tmp_path, monkeypatch):
    """Observation: check_visible_credentials refuses base-URL overrides but
    not an OPENAI_API_KEY in the launching process (the env is scrubbed for
    the child, but without OS confinement /proc still exposes it)."""
    from django_ergo.plugins.paid_codex import PaidCodexPlugin

    for name in ("OPENAI_BASE_URL", "CODEX_API_KEY", "ANTHROPIC_BASE_URL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-fake-not-real")
    from django_ergo.plugins.local_shell import LocalShellError

    with pytest.raises(LocalShellError, match="ambient"):
        PaidCodexPlugin.check_visible_credentials(tmp_path)


# --- 5. route ---------------------------------------------------------------


def test_only_paid_codex_claims_a_spend_bound():
    from django_ergo.bots.agents import AgentManager
    from django_ergo.plugins.local_shell import LocalShellAgents
    from django_ergo.plugins.paid_codex import PaidCodexManager

    bound = [
        cls.__name__
        for cls in _subclasses(AgentManager)
        if getattr(cls, "metered_spend_bound", False)
    ]
    assert bound == ["PaidCodexManager"]
    assert LocalShellAgents.metered_spend_bound is False
    assert PaidCodexManager.metered_spend_bound is True


def _subclasses(cls):
    import importlib

    for module in (
        "django_ergo.plugins.local_shell",
        "django_ergo.plugins.paid_codex",
        "django_ergo.plugins.orca",
    ):
        importlib.import_module(module)
    out, todo = [], [cls]
    while todo:
        for sub in todo.pop().__subclasses__():
            out.append(sub)
            todo.append(sub)
    return out


@pytest.mark.django_db(transaction=True)
def test_refused_paid_launch_releases_unstarted_allocation(held, tmp_path):
    """Refusal without any runtime, token or process releases only allocation."""
    from django_ergo.bots.agents import AgentSpec
    from django_ergo.plugins.local_shell import LocalShellError
    from django_ergo.plugins.paid_codex import PaidCodexPlugin
    from tests.test_model_pools_acceptance import metered_guard

    guard = metered_guard(held)
    allocation = spend_guard.reserve_launch(
        grant_id=held.pk,
        pool="paid",
        provider="openai",
        model=MODEL,
        amount=Decimal("1"),
        user_id=held.grantee_id,
        current=guard.current,
    )
    plugin = PaidCodexPlugin(
        None, {"root": str(tmp_path), "proxy_url": "https://proxy.invalid/v1"}
    )
    plugin.on_load()
    with pytest.raises(LocalShellError, match="no verified"):
        plugin.start_agent(
            __import__("types").SimpleNamespace(
                session=__import__("types").SimpleNamespace(user_id=held.grantee_id)
            ),
            AgentSpec(
                agent="codex",
                workspace="job",
                brief="hi",
                confinement={
                    "agent": "codex",
                    "reservation_id": str(allocation),
                    "grant_id": str(held.pk),
                },
            ),
        )
    row = InferenceRequest.objects.get(pk=allocation)
    assert row.outcome == "not_sent"
    assert spend_guard.spent_and_reserved(held.pk)[1] == 0


# --- 6. migrations ----------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_migration_0066_down_and_up(settings):
    from django.core.management import call_command
    from django.db import connection

    call_command("migrate", "django_ergo", "0065_provider_weekly_budgets", verbosity=0)
    with connection.cursor() as cursor:
        tables = connection.introspection.table_names(cursor)
    assert not any("paidagentlaunch" in t for t in tables)
    call_command("migrate", "django_ergo", verbosity=0)
    with connection.cursor() as cursor:
        tables = connection.introspection.table_names(cursor)
    assert any("paidagentlaunch" in t for t in tables)
    call_command("makemigrations", "django_ergo", "--check", "--dry-run", verbosity=0)


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    "mode",
    [
        "missing",
        "partial",
        "bool",
        "negative",
        "wrong_total",
        "filter",
        "failed",
        "duplicate",
        "mismatch",
        "after",
        "cut",
    ],
)
def test_incomplete_adversarial_usage_keeps_hold(held, mode):  # noqa: C901 -- adversarial matrix
    result = {
        **receipt(),
        "status": "incomplete",
        "incomplete_details": {"reason": "max_output_tokens"},
    }
    if mode == "missing":
        result.pop("usage")
    elif mode == "partial":
        result["usage"].pop("output_tokens")
    elif mode == "bool":
        result["usage"]["input_tokens"] = True
    elif mode == "negative":
        result["usage"]["input_tokens"] = -1
    elif mode == "wrong_total":
        result["usage"]["total_tokens"] = 1
    elif mode == "filter":
        result["incomplete_details"]["reason"] = "content_filter"
    elif mode == "failed":
        result["status"] = "failed"
    event = {"type": "response.incomplete", "response": result}
    if mode == "mismatch":
        event["type"] = "response.completed"
    data = sse(event)
    if mode == "duplicate":
        data += sse(event)
    elif mode == "after":
        data += sse({"type": "response.output_text.delta", "delta": "late"})
    proxy, launch, token, _ = fixture(
        held, lambda _: httpx.Response(200, stream=Chunks(data, cut=mode == "cut"))
    )
    response = proxy.request(
        token, "responses", payload(stream=True, max_output_tokens=128)
    )
    with suppress(spend_guard.SpendDenied, httpx.ReadError):
        b"".join(response.streaming_content)
    row = attempt()
    assert row.cost_status == "unknown" and row.cost_usd is None
    assert spend_guard.spent_and_reserved(held.pk)[1] == row.reserved_usd


@pytest.mark.django_db(transaction=True)
def test_concurrent_retry_ingests_unknown_once_without_forgiving_hold(
    held, monkeypatch
):
    from concurrent.futures import ThreadPoolExecutor

    from django.db import close_old_connections

    from django_ergo.conversation.inference_ledger import reconcile_cost_ledger
    from django_ergo.costs import adapters

    proxy, launch, token, _ = fixture(
        held, lambda _: httpx.Response(200, json={"model": MODEL})
    )
    real = adapters.record_inference_request

    def unavailable(**kwargs):
        msg = "synthetic transient ledger failure"
        raise RuntimeError(msg)

    monkeypatch.setattr(adapters, "record_inference_request", unavailable)
    proxy.request(token, "responses", payload())
    row = attempt()
    assert row.cost_ledger_state == "pending" and row.cost_status == "unknown"
    monkeypatch.setattr(adapters, "record_inference_request", real)

    def retry(_):
        close_old_connections()
        try:
            return reconcile_cost_ledger(row.pk)
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=3) as executor:
        assert all(executor.map(retry, range(3)))
    assert kinds(row.pk) == {"reservation", "unknown"}
    assert (
        CostObservation.objects.filter(
            operation__operation_key=f"inference-request:{row.pk}"
        ).count()
        == 2
    )
    assert spend_guard.spent_and_reserved(held.pk)[1] == row.reserved_usd
