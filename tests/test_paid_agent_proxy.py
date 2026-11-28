"""Mock upstream integration, real PostgreSQL locks/ledger; never paid inference."""

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from decimal import Decimal
from threading import Barrier

import httpx
import pytest
from django.contrib.auth import get_user_model
from django.db import close_old_connections
from django.utils import timezone

from django_ergo.bots import pools
from django_ergo.bots.providers import parse_providers
from django_ergo.conversation import spend_guard
from django_ergo.conversation.models import InferenceRequest
from django_ergo.conversation.models import PaidAgentLaunch
from django_ergo.conversation.models import PaidDispatchState
from django_ergo.conversation.paid_agent_proxy import Proxy
from django_ergo.conversation.paid_agent_proxy import issue
from django_ergo.conversation.paid_agent_proxy import revoke
from django_ergo.costs.ledger import summarize_costs
from tests.test_model_pools import POOLS
from tests.test_model_pools import grant
from tests.test_model_pools_acceptance import metered_guard


@pytest.fixture
def held(db):
    user = get_user_model().objects.create_user("lee")
    return grant(
        parse_providers(POOLS), user, routes=["openai/gpt-6.1-sol"], per_launch_usd="1"
    )


MODEL = "gpt-6.1-sol"


def fixture(held, handler=None, **limits):
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
    launch, token = issue(
        guard=guard,
        reservation_id=allocation,
        account_ref="mock-account",
        authority={},
        **limits,
    )
    sent = []

    def transport(request):
        sent.append(request)
        if handler:
            return handler(request)
        return httpx.Response(200, json=receipt(), headers={"x-request-id": "req_mock"})

    client = httpx.Client(transport=httpx.MockTransport(transport), trust_env=False)
    proxy = Proxy(lambda launch: (guard, "mock-not-a-real-key", "mock-account"), client)
    return proxy, launch, token, sent


def receipt(model=MODEL):
    return {
        "id": "resp_mock",
        "model": model,
        "status": "completed",
        "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
    }


def payload(**extra):
    return {"model": MODEL, "input": "Say hello", "max_output_tokens": 8, **extra}


def attempt():
    return InferenceRequest.objects.get(purpose="paid_agent")


@pytest.mark.django_db(transaction=True)
def test_happy_exact_settlement_and_no_double_count(held):
    proxy, launch, token, sent = fixture(held)
    response = proxy.request(token, "responses", payload())
    assert response.status_code == 200 and len(sent) == 1
    row = attempt()
    assert row.actual_model == MODEL and row.upstream_request_id == "req_mock"
    assert row.cost_usd == proxy.resolver(launch)[0].price.exact_cost(10, 0, 0, 5)
    spent, reserved = spend_guard.spent_and_reserved(held.pk)
    assert spent == row.cost_usd and reserved == 0
    summary = summarize_costs(held.grantee)
    assert (
        next(b for b in summary if b["kind"] == "estimate")["known_amount"]
        == row.cost_usd
    )
    allocation = InferenceRequest.objects.get(pk=launch.reservation_id)
    assert allocation.outcome == "not_sent"
    assert json.loads(sent[0].content)["store"] is False
    assert launch.token_digest != token


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    "denial",
    [
        "model",
        "provider",
        "pause",
        "cap",
        "expired",
        "revoked",
        "endpoint",
        "key",
        "base_url",
        "account",
        "grant",
        "unknown_tariff",
    ],
)
def test_denials_before_upstream_bytes(held, denial):  # noqa: C901 - denial matrix
    proxy, launch, token, sent = fixture(held)
    body = payload()
    path = "responses"
    if denial == "model":
        body["model"] = "disallowed"
    elif denial == "provider":
        pools.set_provider_enabled("openai", parse_providers(POOLS), False, version=0)
    elif denial == "pause":
        PaidDispatchState.objects.update_or_create(pk=1, defaults={"paused": True})
    elif denial == "cap":
        PaidAgentLaunch.objects.filter(pk=launch.pk).update(
            cap_usd=Decimal("0.0000000001")
        )
    elif denial == "expired":
        PaidAgentLaunch.objects.filter(pk=launch.pk).update(
            expires_at=timezone.now() - timedelta(seconds=1)
        )
    elif denial == "revoked":
        revoke(launch.pk)
    elif denial == "endpoint":
        path = "https://api.openai.com/v1/responses"
    elif denial == "key":
        body["api_key"] = "mock-direct-key"
    elif denial == "base_url":
        body["base_url"] = "https://api.openai.com/v1"
    elif denial == "account":
        guard = proxy.resolver(launch)[0]
        proxy.resolver = lambda launch: (guard, "mock", "other-account")
    elif denial == "grant":
        held.revoked_at = timezone.now()
        held.save(update_fields=["revoked_at"])
    elif denial == "unknown_tariff":
        guard = proxy.resolver(launch)[0]
        from dataclasses import replace

        current = guard.current
        guard.current = lambda grant: replace(current(grant), prices={})
    with pytest.raises((spend_guard.SpendDenied, spend_guard.UnsupportedRequest)):
        proxy.request(token, path, body)
    assert sent == []
    assert not InferenceRequest.objects.filter(purpose="paid_agent").exists()


@pytest.mark.django_db(transaction=True)
def test_retries_count_as_distinct_attempts_and_revoke(held):
    proxy, launch, token, sent = fixture(held, max_attempts=2)
    proxy.request(token, "responses", payload())
    proxy.request(token, "responses", payload())
    with pytest.raises(spend_guard.SpendDenied):
        proxy.request(token, "responses", payload())
    assert len(sent) == 2
    assert list(
        launch.requests.order_by("attempt").values_list("attempt", flat=True)
    ) == [1, 2]
    launch.refresh_from_db()
    assert launch.revoked_at and launch.total_reserved_usd == sum(
        r.reserved_usd for r in launch.requests.all()
    )


@pytest.mark.django_db(transaction=True)
def test_parallel_requests_race_last_reservation(held):
    proxy, launch, token, sent = fixture(held)
    # Calculate a reservation, then leave exactly one more attempt of capacity.
    proxy.request(token, "responses", payload())
    amount = attempt().reserved_usd
    PaidAgentLaunch.objects.filter(pk=launch.pk).update(cap_usd=amount * 2)
    barrier = Barrier(2)

    def send(_):
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            try:
                proxy.request(token, "responses", payload())
                return "sent"
            except spend_guard.SpendDenied:
                return "denied"
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as executor:
        assert sorted(executor.map(send, range(2))) == ["denied", "sent"]
    assert len(sent) == 2 and launch.requests.count() == 2


class Chunks(httpx.SyncByteStream):
    def __init__(self, data, cut=False):
        self.data = data
        self.cut = cut

    def __iter__(self):
        yield self.data
        if self.cut:
            msg = "mock cut"
            raise httpx.ReadError(msg)


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    "mode",
    ["complete", "cut", "no_usage", "partial_usage", "downstream_close", "never_read"],
)
def test_streaming_completion_or_unknown_hold(held, mode):
    data = b'data: {"type":"response.output_text.delta","delta":"Hi"}\n\n'
    if mode in {"complete", "cut", "partial_usage"}:
        result = receipt()
        if mode == "partial_usage":
            result["usage"].pop("output_tokens")
        data += (
            "data: "
            + json.dumps({"type": "response.completed", "response": result})
            + "\n\n"
        ).encode()
    proxy, launch, token, sent = fixture(
        held, lambda req: httpx.Response(200, stream=Chunks(data, mode == "cut"))
    )
    response = proxy.request(token, "responses", payload(stream=True))
    if mode == "never_read":
        response.close()
    elif mode == "downstream_close":
        iterator = iter(response.streaming_content)
        next(iterator)
        response.close()
    elif mode == "cut":
        with pytest.raises(httpx.ReadError):
            b"".join(response.streaming_content)
    else:
        assert b"Hi" in b"".join(response.streaming_content)
    row = attempt()
    assert len(sent) == 1
    if mode == "complete":
        assert row.cost_status == "priced"
        assert spend_guard.spent_and_reserved(held.pk)[1] == 0
    else:
        assert row.cost_status == "unknown" and row.cost_usd is None
        assert spend_guard.spent_and_reserved(held.pk)[1] == row.reserved_usd


@pytest.mark.django_db(transaction=True)
def test_actual_model_mismatch_is_recorded_and_retains_hold(held):
    proxy, launch, token, sent = fixture(
        held, lambda req: httpx.Response(200, json=receipt("different-model"))
    )
    proxy.request(token, "responses", payload())
    row = attempt()
    assert row.model == MODEL and row.actual_model == "different-model"
    assert row.cost_status == "unknown" and row.cost_usd is None
    assert spend_guard.spent_and_reserved(held.pk)[1] == row.reserved_usd
    with pytest.raises(spend_guard.SpendDenied):
        proxy.request(token, "responses", payload())
    assert len(sent) == 1


@pytest.mark.django_db(transaction=True)
def test_chat_and_stream_usage(held):
    data = {"model": MODEL, "usage": {"prompt_tokens": 10, "completion_tokens": 5}}
    stream = ("data: " + json.dumps(data) + "\n\ndata: [DONE]\n\n").encode()
    proxy, launch, token, sent = fixture(
        held, lambda req: httpx.Response(200, stream=Chunks(stream))
    )
    response = proxy.request(
        token, "chat/completions", {"model": MODEL, "messages": [], "stream": True}
    )
    b"".join(response.streaming_content)
    assert attempt().cost_status == "priced"
    outgoing = json.loads(sent[0].content)
    assert outgoing["stream_options"] == {"include_usage": True}
    assert outgoing["max_completion_tokens"] > 0


@pytest.mark.django_db(transaction=True)
def test_two_launches_race_shared_grant_last_dollars(held):
    # A response with no usage keeps the complete reservation outstanding.
    proxy, launch, token, sent = fixture(held)
    proxy.request(token, "responses", payload())
    amount = attempt().reserved_usd
    other, _, other_token, other_sent = fixture(
        held, lambda req: httpx.Response(200, json={"model": MODEL})
    )
    spent, _ = spend_guard.spent_and_reserved(held.pk)
    guard = metered_guard(held)
    spend_guard.reserve(
        grant_id=held.pk,
        provider="openai",
        model=MODEL,
        amount=held.budget_usd - spent - amount,
        user_id=held.grantee_id,
        fields={},
        current=guard.current,
    )
    proxy.client = httpx.Client(
        transport=httpx.MockTransport(
            lambda req: (sent.append(req), httpx.Response(200, json={"model": MODEL}))[
                1
            ]
        )
    )
    barrier = Barrier(2)

    def send(pair):
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            try:
                pair[0].request(pair[1], "responses", payload())
                return "sent"
            except spend_guard.SpendDenied:
                return "denied"
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as executor:
        assert sorted(executor.map(send, [(proxy, token), (other, other_token)])) == [
            "denied",
            "sent",
        ]
    assert len(sent) + len(other_sent) == 2  # one seed, one newly authorized attempt
    spent, reserved = spend_guard.spent_and_reserved(held.pk)
    assert spent + reserved == held.budget_usd


@pytest.mark.django_db(transaction=True)
def test_expiry_crash_recovery_never_releases_unknown(held):
    from django.core.management import call_command

    proxy, launch, token, sent = fixture(held)
    _, pk, *_ = proxy.begin(token, "responses", payload())
    assert not sent  # crash between committed reservation and transport
    PaidAgentLaunch.objects.filter(pk=launch.pk).update(
        expires_at=timezone.now() - timedelta(seconds=1)
    )
    call_command("expire_paid_agent_launches")
    row = InferenceRequest.objects.get(pk=pk)
    assert row.cost_status == "unknown" and row.cost_usd is None
    assert spend_guard.spent_and_reserved(held.pk)[1] == row.reserved_usd


@pytest.mark.django_db(transaction=True)
def test_http_auth_denial_never_constructs_transport(held, monkeypatch, settings):
    from django.test import RequestFactory

    from django_ergo.conversation.paid_agent_proxy import proxy_view

    calls = []
    settings.PAID_AGENT_PROXY_RESOLVER = (
        "django_ergo.bots.paid_agent_authority.resolve_launch"
    )
    monkeypatch.setattr(
        httpx.Client, "send", lambda *args, **kwargs: calls.append(True)
    )
    response = proxy_view(
        RequestFactory().post(
            "/v1/responses",
            data=json.dumps(payload()),
            content_type="application/json",
            HTTP_AUTHORIZATION="Bearer invalid",
        ),
        "responses",
    )
    assert response.status_code == 403 and calls == []


@pytest.mark.django_db(transaction=True)
def test_codex_0171_inline_namespaced_tools_are_bounded(held):
    proxy, _, token, sent = fixture(held)
    body = payload(
        client_metadata={"client": "codex"},
        prompt_cache_key="mock-thread",
        include=["reasoning.encrypted_content"],
        input=[
            {
                "type": "additional_tools",
                "id": "inline",
                "role": "developer",
                "tools": [
                    {
                        "type": "namespace",
                        "name": "functions",
                        "tools": [
                            {
                                "type": "custom",
                                "name": "apply_patch",
                                "format": {"type": "text"},
                            },
                        ],
                    }
                ],
            },
            {
                "type": "message",
                "id": "inline-message",
                "role": "user",
                "content": [{"type": "input_text", "text": "hello"}],
            },
        ],
    )
    proxy.request(token, "responses", body)
    forwarded = json.loads(sent[0].content)
    assert "include" not in forwarded
    assert all("id" not in item for item in forwarded["input"])
    assert attempt().cost_status == "priced"


@pytest.mark.django_db(transaction=True)
def test_hosted_tool_nested_in_namespace_denied_before_transport(held):
    proxy, _, token, sent = fixture(held)
    body = payload(
        input=[
            {
                "type": "additional_tools",
                "tools": [{"type": "namespace", "tools": [{"type": "web_search"}]}],
            }
        ]
    )
    with pytest.raises(spend_guard.SpendDenied):
        proxy.request(token, "responses", body)
    assert not sent


@pytest.mark.django_db(transaction=True)
def test_installed_codex_responses_wire_against_mock_only(held, tmp_path):
    import os
    import shutil
    import subprocess
    from http.server import BaseHTTPRequestHandler
    from http.server import ThreadingHTTPServer
    from threading import Thread

    executable = shutil.which("codex")
    if not executable:
        pytest.skip("Codex CLI not installed; protocol fixture is still tested")
    message = {
        "id": "msg_mock",
        "type": "message",
        "role": "assistant",
        "status": "completed",
        "content": [{"type": "output_text", "text": "Hello", "annotations": []}],
    }
    result = {**receipt(), "output": [message]}
    events = [
        {
            "type": "response.created",
            "response": {**result, "status": "in_progress", "output": []},
        },
        {
            "type": "response.output_item.added",
            "output_index": 0,
            "item": {**message, "status": "in_progress", "content": []},
        },
        {
            "type": "response.output_text.delta",
            "item_id": "msg_mock",
            "output_index": 0,
            "content_index": 0,
            "delta": "Hello",
        },
        {"type": "response.output_item.done", "output_index": 0, "item": message},
        {"type": "response.completed", "response": result},
    ]
    data = "".join(
        f"event: {event['type']}\ndata: {json.dumps(event)}\n\n" for event in events
    ).encode()
    proxy, launch, token, sent = fixture(
        held,
        lambda req: httpx.Response(
            200, stream=Chunks(data), headers={"x-request-id": "req_cli_mock"}
        ),
    )
    errors = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            close_old_connections()
            response = None
            try:
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                assert self.headers["Authorization"] == f"Bearer {token}"
                response = proxy.request(token, "responses", body)
                self.send_response(response.status_code)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                for chunk in response.streaming_content:
                    self.wfile.write(chunk)
                    self.wfile.flush()
            except Exception as exc:  # noqa: BLE001 - report safe exception class, not credentials
                errors.append(type(exc).__name__)
                self.send_error(403)
            finally:
                if response:
                    response.close()
                close_old_connections()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    home = tmp_path / "home"
    home.mkdir()
    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(home),
        "CODEX_HOME": str(home),
        "ERGO_PROXY_TOKEN": token,
        "LANG": "C.UTF-8",
    }
    config = {
        "model_provider": "ergo_mock",
        "model_providers.ergo_mock.name": "Unpaid test",
        "model_providers.ergo_mock.base_url": f"http://127.0.0.1:{server.server_port}/v1",
        "model_providers.ergo_mock.env_key": "ERGO_PROXY_TOKEN",
        "model_providers.ergo_mock.wire_api": "responses",
        "model_providers.ergo_mock.requires_openai_auth": False,
        "model_providers.ergo_mock.supports_websockets": False,
        "model_providers.ergo_mock.request_max_retries": 0,
        "model_providers.ergo_mock.stream_max_retries": 0,
    }
    argv = [
        executable,
        "exec",
        "--json",
        "--skip-git-repo-check",
        "--sandbox",
        "read-only",
        "--model",
        MODEL,
    ]
    for key, value in config.items():
        argv.extend(["-c", f"{key}={json.dumps(value)}"])
    argv.append("Reply hello. Do not use tools.")
    try:
        completed = subprocess.run(
            argv, cwd=tmp_path, env=env, capture_output=True, timeout=45, check=False
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    assert errors == []
    assert completed.returncode == 0, completed.stdout.decode(errors="replace")
    assert b'"type":"turn.completed"' in completed.stdout
    assert len(sent) == 1
    assert attempt().upstream_request_id == "req_cli_mock"
    assert attempt().cost_status == "priced"
