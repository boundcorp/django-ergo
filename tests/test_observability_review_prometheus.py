"""Regression coverage for adversarial Prometheus responses."""

from datetime import UTC
from datetime import datetime

import pytest

from django_ergo.bots.tools import ToolContext
from django_ergo.observability import text
from django_ergo.observability.errors import ObservabilityError
from django_ergo.observability.profile import Profile
from django_ergo.observability.prometheus import MAX_SAMPLES
from django_ergo.observability.prometheus import PromClient
from django_ergo.observability.prometheus import normalize
from django_ergo.observability.timeutil import load_tz
from django_ergo.plugins.observability import ObservabilityPlugin
from tests.observability_server import FakeServer
from tests.observability_server import Reply
from tests.observability_server import success

TOKEN = "FAKE_REVIEW_TOKEN"
BASE = "/api/datasources/proxy/uid/prom/api/v1"
NOW = datetime(2025, 6, 15, tzinfo=UTC)


@pytest.fixture
def server():
    fixture = FakeServer()
    yield fixture
    fixture.close()


def client(server):
    profile = Profile.from_config(
        "test", {"grafana_url": server.url, "datasources": ["prom"]}
    )
    return PromClient(profile, "prom", token=TOKEN, clock=lambda: NOW)


def test_json_error_redacted_at_tool_boundary(server, monkeypatch):
    monkeypatch.setenv("REVIEW_TEST_TOKEN", TOKEN)
    server.routes[BASE + "/query"] = Reply(
        200, {"status": "error", "errorType": TOKEN, "error": "Bearer " + TOKEN}
    )
    plugin = ObservabilityPlugin(
        None,
        {
            "profiles": {
                "main": {
                    "grafana_url": server.url,
                    "datasources": ["prom"],
                    "token_env": "REVIEW_TEST_TOKEN",
                }
            }
        },
    )
    out = plugin.toolkits(ToolContext())[0].execute_tool(
        "ergo_prom_query", {"expr": "up", "tz": "UTC"}
    )
    assert out.startswith("ERROR [invalid_query]")
    assert TOKEN not in out


def test_query_and_target_output_redacts_upstream_strings(server):
    c = client(server)
    server.routes[BASE + "/query"] = success(
        {
            "resultType": "vector",
            "result": [{"metric": {TOKEN: TOKEN}, "value": [1, "1"]}],
        },
        warnings=[TOKEN],
    )
    result, _, _ = c.instant("up", tz="UTC")
    assert TOKEN not in repr(result)
    server.routes[BASE + "/query"] = success(
        {"resultType": "string", "result": [1, TOKEN]}
    )
    assert TOKEN not in repr(c.instant("up", tz="UTC")[0])
    server.routes[BASE + "/targets"] = success(
        {
            "activeTargets": [
                {
                    "labels": {"job": TOKEN, "instance": TOKEN},
                    "health": TOKEN,
                    "lastScrape": TOKEN,
                    "lastError": TOKEN,
                }
            ]
        }
    )
    assert TOKEN not in repr(c.targets())


def test_mixed_histograms_preserve_float_samples_and_annotate():
    result = normalize(
        {
            "resultType": "matrix",
            "result": [
                {
                    "metric": {"job": "mixed"},
                    "values": [[1, "42"]],
                    "histograms": [[16, {}]],
                }
            ],
        },
        "range",
        15,
    )
    assert result.series[0].points == [(1, 42)]
    assert result.histogram_series == 1
    assert result.total_samples == 2
    assert "UNSUPPORTED" in text.format_query(
        result, query="q", source="s", tz=load_tz("UTC")
    )


def test_histogram_samples_count_toward_global_bound():
    samples = [[i, {}] for i in range(1000)]
    rows = [{"metric": {}, "histograms": samples} for _ in range(100)]
    assert (
        normalize({"resultType": "matrix", "result": rows}, "range").total_samples
        == MAX_SAMPLES
    )
    rows[-1] = {"metric": {}, "histograms": samples + [[1000, {}]]}
    with pytest.raises(ObservabilityError) as exc:
        normalize({"resultType": "matrix", "result": rows}, "range")
    assert exc.value.code == "bounds_exceeded"


@pytest.mark.parametrize(("count", "ok"), [(2000, True), (2001, False)])
def test_actual_per_series_point_bound(count, ok):
    data = {
        "resultType": "matrix",
        "result": [{"metric": {}, "values": [[1, "1"]] * count}],
    }
    if ok:
        assert normalize(data, "range").total_samples == count
    else:
        with pytest.raises(ObservabilityError, match="points per series"):
            normalize(data, "range")


def test_default_range_limit_applies_to_returned_samples(server):
    server.routes[BASE + "/query_range"] = success(
        {
            "resultType": "matrix",
            "result": [{"metric": {}, "values": [[1, "1"]] * 1001}],
        }
    )
    with pytest.raises(ObservabilityError, match="points per series"):
        client(server).range("up", tz="UTC", window="1h")


def test_direct_source_identity_is_host_owned(server):
    profile = Profile.from_config(
        "direct",
        {"mode": "direct", "prometheus_url": server.url, "datasources": ["allowed"]},
    )
    assert PromClient(profile, None).datasource == "allowed"
    assert PromClient(profile, "allowed").datasource == "allowed"
    with pytest.raises(ObservabilityError):
        PromClient(profile, "forbidden")
    anonymous = Profile.from_config(
        "direct", {"mode": "direct", "prometheus_url": server.url}
    )
    assert PromClient(anonymous, None).datasource == ""
    with pytest.raises(ObservabilityError):
        PromClient(anonymous, "forbidden")
    with pytest.raises(ValueError, match="datasource"):
        Profile.from_config(
            "direct",
            {"mode": "direct", "prometheus_url": server.url, "datasources": ["a", "b"]},
        )
    assert not server.requests


@pytest.mark.parametrize(
    "data",
    [
        {"resultType": "scalar", "result": None},
        {"resultType": "string", "result": [1]},
        {"resultType": "vector", "result": [None]},
        {"resultType": "vector", "result": [{"metric": [], "value": [1]}]},
        {"resultType": "matrix", "result": [{"metric": {}, "values": [[1]]}]},
        {
            "resultType": "vector",
            "result": [{"metric": {}, "value": [float("nan"), "1"]}],
        },
        {"resultType": "vector", "result": [{"metric": {}, "value": [1e300, "1"]}]},
        {"resultType": "matrix", "result": [{"metric": {}, "histograms": [[1, None]]}]},
    ],
)
def test_malformed_nested_results_are_typed(data):
    with pytest.raises(ObservabilityError) as exc:
        normalize(data, "range")
    assert exc.value.code == "upstream_unavailable"


@pytest.mark.parametrize(
    "data",
    [
        {"activeTargets": None},
        {"activeTargets": [None]},
        {"activeTargets": [{"labels": []}]},
        {"activeTargets": [{"labels": {}, "health": []}]},
    ],
)
def test_malformed_targets_are_typed(server, data):
    server.routes[BASE + "/targets"] = success(data)
    with pytest.raises(ObservabilityError) as exc:
        client(server).targets()
    assert exc.value.code == "upstream_unavailable"
