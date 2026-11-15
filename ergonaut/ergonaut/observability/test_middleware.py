from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from asgiref.sync import async_to_sync, sync_to_async
from django.db import connection
from django.http import HttpResponse, StreamingHttpResponse
from django.test import AsyncClient, Client, RequestFactory
from django.urls import path

from ergonaut.observability import middleware

_METRICS = (
    "http_requests_total",
    "http_request_duration_seconds",
    "http_request_db_queries",
    "http_request_db_duration_seconds",
    "http_request_phase_duration_seconds",
    "http_response_size_bytes",
)


@pytest.fixture
def metrics(monkeypatch, settings):
    settings.TELEMETRY_METRICS_ENABLED = True
    result = {}
    for name in _METRICS:
        result[name] = Mock()
        monkeypatch.setattr(middleware, name, result[name])
    return result


def request(route=None, method="GET"):
    result = RequestFactory().generic(
        method,
        "/api/bots/private-bot/sessions/private-session?text=private-message",
        HTTP_HOST="private-user.example.com",
    )
    if route is not None:
        result.resolver_match = SimpleNamespace(route=route)
    return result


@pytest.mark.parametrize(
    ("route", "expected"),
    [
        ("api/bots/<bot>/sessions/<uuid:session_id>", "api/bots/{bot}/sessions/{session_id}"),
        ("api/bots/<str:bot>/source/<path:path>", "api/bots/{bot}/source/{path}"),
        (None, "unmatched"),
    ],
)
def test_labels_use_templates_not_request_data(metrics, route, expected):
    response = middleware.PrometheusMetricsMiddleware(lambda req: HttpResponse("private-response"))(
        request(route, "PRIVATE-METHOD")
    )
    expected_labels = {"host": "all", "method": "OTHER", "route": expected}
    metrics["http_requests_total"].labels.assert_called_once_with(**expected_labels, status_code="200")
    for name in _METRICS:
        if name in {"http_requests_total", "http_request_phase_duration_seconds"}:
            continue
        metrics[name].labels.assert_called_once_with(**expected_labels)
    metrics["http_response_size_bytes"].labels.return_value.observe.assert_called_once_with(16)
    evidence = repr([metric.mock_calls for metric in metrics.values()]) + response["Server-Timing"]
    for secret in ("private-bot", "private-session", "private-message", "private-user", "private-response"):
        assert secret not in evidence


def test_header_preserves_existing_timing_and_aggregates_allowlisted_phases(metrics, monkeypatch):
    ticks = iter(range(20))
    monkeypatch.setattr(middleware.time, "perf_counter", lambda: next(ticks))

    def view(req):
        with middleware.request_phase("history"):
            pass
        with middleware.request_phase("history"):
            pass
        with middleware.request_phase("private-session"):
            pass
        response = HttpResponse("ok")
        response["Server-Timing"] = "cache;dur=1"
        return response

    response = middleware.PrometheusMetricsMiddleware(view)(request())
    assert response["Server-Timing"] == (
        'cache;dur=1, total;dur=5000.000, db;dur=0.000, db_queries;desc="0", history;dur=2000.000'
    )
    metrics["http_request_phase_duration_seconds"].labels.assert_called_once_with(
        host="all", method="GET", route="unmatched", phase="history"
    )
    metrics["http_request_phase_duration_seconds"].labels.return_value.observe.assert_called_once_with(2.0)
    # A scope after the response must not leak into the next request.
    with middleware.request_phase("sidebar"):
        pass
    assert metrics["http_request_phase_duration_seconds"].labels.call_count == 1


def test_disabled_is_a_noop(metrics, settings, monkeypatch):
    settings.TELEMETRY_METRICS_ENABLED = False
    monkeypatch.setattr(middleware.time, "perf_counter", Mock(side_effect=AssertionError("timing disabled")))
    monkeypatch.setattr(middleware.connections, "all", Mock(side_effect=AssertionError("wrappers disabled")))
    response = HttpResponse("ok")

    def view(req):
        with middleware.request_phase("history"):
            return response

    assert middleware.PrometheusMetricsMiddleware(view)(request()) is response
    assert "Server-Timing" not in response
    for metric in metrics.values():
        assert not metric.mock_calls


def test_streaming_body_is_not_consumed(metrics):
    consumed = []

    def chunks():
        consumed.append(True)
        yield b"private attachment"

    response = StreamingHttpResponse(chunks())
    returned = middleware.PrometheusMetricsMiddleware(lambda req: response)(request())
    assert returned is response
    assert not consumed
    assert "total;dur=" in response["Server-Timing"]
    assert not metrics["http_response_size_bytes"].mock_calls
    assert b"".join(response.streaming_content) == b"private attachment"


@pytest.mark.django_db
def test_database_aggregation_retains_only_count_and_duration(metrics):
    connection.ensure_connection()
    before = list(connection.execute_wrappers)

    def view(req):
        with connection.cursor() as cursor:
            cursor.execute("SELECT %s::text", ["private-query-payload"])
            assert cursor.fetchone() == ("private-query-payload",)
            cursor.execute("SELECT 2")
            assert cursor.fetchone() == (2,)
        return HttpResponse("ok")

    response = middleware.PrometheusMetricsMiddleware(view)(request("api/sessions/<session_id>"))
    metrics["http_request_db_queries"].labels.return_value.observe.assert_called_once_with(2)
    duration = metrics["http_request_db_duration_seconds"].labels.return_value.observe.call_args.args[0]
    assert duration >= 0
    assert 'db_queries;desc="2"' in response["Server-Timing"]
    assert connection.execute_wrappers == before
    evidence = repr([metric.mock_calls for metric in metrics.values()]) + response["Server-Timing"]
    assert "SELECT" not in evidence
    assert "private-query-payload" not in evidence
    with connection.cursor() as cursor:
        cursor.execute("SELECT 3")
    assert metrics["http_request_db_queries"].labels.return_value.observe.call_count == 1


def test_database_failures_are_counted_without_retaining_queries(metrics, monkeypatch):
    ticks = iter(range(10))
    monkeypatch.setattr(middleware.time, "perf_counter", lambda: next(ticks))

    def view(req):
        wrapper = connection.execute_wrappers[-1]
        with pytest.raises(ValueError, match="private failure"):
            wrapper(Mock(side_effect=ValueError("private failure")), "private SQL", ["private data"], False, {})
        return HttpResponse("ok")

    response = middleware.PrometheusMetricsMiddleware(view)(request())
    assert 'db_queries;desc="1"' in response["Server-Timing"]
    metrics["http_request_db_duration_seconds"].labels.return_value.observe.assert_called_once_with(1.0)
    assert "private" not in repr([metric.mock_calls for metric in metrics.values()])


def test_exception_cleans_up_wrappers_and_phase_context(metrics):
    before = list(connection.execute_wrappers)

    def fail(req):
        with middleware.request_phase("history"):
            raise ValueError("view failed")

    with pytest.raises(ValueError, match="view failed"):
        middleware.PrometheusMetricsMiddleware(fail)(request())
    assert connection.execute_wrappers == before
    assert middleware._request_timing.get() is None
    with middleware.request_phase("history"):
        pass
    assert not metrics["http_request_phase_duration_seconds"].mock_calls


def _query():
    with middleware.request_phase("history"), connection.cursor() as cursor:
        cursor.execute("SELECT 1")
        return cursor.fetchone()[0]


def _sync_view(req, session_id):
    return HttpResponse(str(_query()))


async def _async_view(req, session_id):
    return HttpResponse(str(await sync_to_async(_query)()))


urlpatterns = [
    path("timing/sync/<uuid:session_id>", _sync_view),
    path("timing/async/<uuid:session_id>", _async_view),
]


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("asgi", [False, True])
@pytest.mark.parametrize("endpoint", ["sync", "async"])
def test_sync_and_async_requests_capture_thread_sensitive_queries(metrics, settings, asgi, endpoint):
    settings.MIDDLEWARE = ["ergonaut.observability.middleware.PrometheusMetricsMiddleware"]
    settings.ROOT_URLCONF = __name__
    url = f"/timing/{endpoint}/024e6e1f-6961-466a-982a-2af1b4529f11"
    response = async_to_sync(AsyncClient().get)(url) if asgi else Client().get(url)
    assert response.status_code == 200
    assert response.content == b"1"
    assert 'db_queries;desc="1"' in response["Server-Timing"]
    assert "history;dur=" in response["Server-Timing"]
    metrics["http_request_db_queries"].labels.assert_called_once_with(
        host="all", method="GET", route=f"timing/{endpoint}/{{session_id}}"
    )
    metrics["http_request_db_queries"].labels.return_value.observe.assert_called_once_with(1)
