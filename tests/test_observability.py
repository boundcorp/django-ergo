"""The observability library: bounded HTTP, windows, normalization, links, charts."""

from __future__ import annotations

import json
import re
import struct
from datetime import UTC
from datetime import datetime
from urllib.parse import parse_qs
from urllib.parse import urlsplit

import pytest

from django_ergo.observability import charts
from django_ergo.observability import text
from django_ergo.observability.errors import ObservabilityError
from django_ergo.observability.grafana import GrafanaClient
from django_ergo.observability.grafana import dashboard_link
from django_ergo.observability.http import BoundedClient
from django_ergo.observability.http import Limits
from django_ergo.observability.http import Response
from django_ergo.observability.profile import Profile
from django_ergo.observability.prometheus import MAX_SAMPLES
from django_ergo.observability.prometheus import MAX_SERIES
from django_ergo.observability.prometheus import PromClient
from django_ergo.observability.prometheus import normalize
from django_ergo.observability.timeutil import load_tz
from django_ergo.observability.timeutil import resolve_window
from tests.observability_server import FakeServer
from tests.observability_server import Reply
from tests.observability_server import matrix
from tests.observability_server import success
from tests.observability_server import vector

NOW = datetime(2025, 6, 15, 12, 0, 0, tzinfo=UTC)
TOKEN = "glsa_SECRET_TOKEN_VALUE"
PROXY = "/api/datasources/proxy/uid/prom/api/v1"


@pytest.fixture
def server():
    srv = FakeServer()
    yield srv
    srv.close()


@pytest.fixture
def other():
    srv = FakeServer()
    yield srv
    srv.close()


def profile(server, **extra):
    return Profile.from_config(
        "test",
        {
            "grafana_url": server.url,
            "public_url": "https://grafana.example.com",
            "datasources": ["prom"],
            **extra,
        },
    )


def prom(server, **kw):
    return PromClient(profile(server), "prom", token=TOKEN, clock=lambda: NOW, **kw)


def fail(fn, *args, **kwargs) -> ObservabilityError:
    with pytest.raises(ObservabilityError) as info:
        fn(*args, **kwargs)
    return info.value


# -- HTTP bounds, errors and credentials -----------------------------------------


def test_401_and_403_are_distinct_and_never_fall_back_to_direct(server, other):
    prof = Profile.from_config(
        "t",
        {
            "grafana_url": server.url,
            "prometheus_url": other.url,
            "datasources": ["prom"],
        },
    )
    other.routes["/api/v1/query"] = success(vector(({}, "1")))
    client = PromClient(prof, "prom", token=TOKEN, clock=lambda: NOW)
    for status, code in ((401, "unauthorized"), (403, "forbidden")):
        server.routes[f"{PROXY}/query"] = Reply(status, {"message": f"bad {TOKEN}"})
        err = fail(client.instant, "up", tz="UTC")
        assert err.code == code
        assert TOKEN not in str(err)
    assert other.requests == []


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (404, "not_found"),
        (400, "invalid_query"),
        (422, "invalid_query"),
        (429, "rate_limited"),
        (500, "upstream_unavailable"),
        (503, "upstream_unavailable"),
    ],
)
def test_upstream_statuses_map_to_typed_errors(server, status, code):
    server.routes[f"{PROXY}/query"] = Reply(
        status, {"status": "error", "error": "parse error at 1:3"}
    )
    err = fail(prom(server).instant, "up", tz="UTC")
    assert err.code == code


def test_invalid_query_carries_the_parse_error_but_redacts_the_token(server):
    server.routes[f"{PROXY}/query"] = Reply(422, {"error": f"bad expr {TOKEN} here"})
    err = fail(prom(server).instant, "up(", tz="UTC")
    assert "bad expr" in err.message
    assert TOKEN not in err.message


def test_html_is_never_returned(server):
    server.routes[f"{PROXY}/query"] = Reply(
        502, b"<html><body>secret-banner</body></html>", ctype="text/html"
    )
    err = fail(prom(server).instant, "up", tz="UTC")
    assert err.code == "upstream_unavailable"
    assert "secret-banner" not in str(err)
    server.routes[f"{PROXY}/query"] = Reply(
        200, b"<html>login page</html>", ctype="text/html"
    )
    err = fail(prom(server).instant, "up", tz="UTC")
    assert err.code == "upstream_unavailable"
    assert "login page" not in str(err)


def test_slow_upstream_times_out(server):
    server.routes[f"{PROXY}/query"] = Reply(200, {}, delay=1.0)
    client = prom(server, limits=Limits(read_timeout=0.2, total_timeout=0.5))
    assert fail(client.instant, "up", tz="UTC").code == "timeout"


def test_connection_refused_is_upstream_unavailable():
    dead = FakeServer()
    url = dead.url
    dead.close()
    prof = Profile.from_config("t", {"grafana_url": url, "datasources": ["prom"]})
    client = PromClient(prof, "prom", token=TOKEN, clock=lambda: NOW)
    assert fail(client.instant, "up", tz="UTC").code == "upstream_unavailable"


def test_body_size_limit_is_exact_and_applies_after_decompression(server):
    path = re.compile(r"/api/search")
    client = BoundedClient(
        server.url, allowed=[path], limits=Limits(max_body_bytes=1000)
    )
    server.routes["/api/search"] = Reply(200, b"x" * 1000)
    assert len(client.get("/api/search").body) == 1000
    server.routes["/api/search"] = Reply(200, b"x" * 1001)
    assert fail(client.get, "/api/search").code == "bounds_exceeded"
    server.routes["/api/search"] = Reply(200, b"0" * 1_000_000, gzip_body=True)
    assert fail(client.get, "/api/search").code == "bounds_exceeded"
    server.routes["/api/search"] = Reply(200, b"y" * 1000, gzip_body=True)
    assert client.get("/api/search").body == b"y" * 1000


def test_redirects_are_not_followed_and_credentials_stay_home(server, other):
    other.routes["/api/search"] = Reply(200, [])
    server.routes["/api/search"] = Reply(
        302, b"", headers={"Location": f"{other.url}/api/search"}
    )
    client = BoundedClient(server.url, allowed=[re.compile("/api/search")], token=TOKEN)
    assert fail(client.get, "/api/search").code == "upstream_unavailable"
    assert other.requests == []


def test_only_allowlisted_get_paths_are_reachable(server):
    client = BoundedClient(server.url, allowed=[re.compile("/api/search")], token=TOKEN)
    for path in (
        "/api/admin/users",
        "/api/search/../admin",
        "//evil.example/api/search",
        "http://evil.example/api/search",
        "/api/search?x=1",
        "api/search",
    ):
        assert fail(client.get, path).code == "invalid_query"
    assert server.requests == []


def test_token_goes_only_to_the_configured_origin_as_bearer(server):
    server.routes[f"{PROXY}/query"] = success(vector(({}, "1")))
    prom(server).instant("up", tz="UTC")
    assert server.requests[0]["headers"]["Authorization"] == f"Bearer {TOKEN}"


def test_datasource_allowlist_is_enforced_before_any_request(server):
    for uid in ("other", "../prom", "prom/../../x"):
        err = fail(PromClient, profile(server), uid, token=TOKEN)
        assert err.code == "not_configured"
    assert server.requests == []
    assert PromClient(profile(server), None, token=TOKEN).datasource == "prom"


def test_direct_mode_sends_no_credential(server):
    prof = Profile.from_config("d", {"mode": "direct", "prometheus_url": server.url})
    server.routes["/api/v1/query"] = success(vector(({}, "1")))
    PromClient(prof, None, token="ignored", clock=lambda: NOW).instant("up", tz="UTC")
    assert "Authorization" not in server.requests[0]["headers"]


# -- windows, steps, timezone ------------------------------------------------------


def test_tz_is_required_and_validated():
    for bad in (None, "", "Mars/Olympus", "EST5EDT/x"):
        assert (
            fail(resolve_window, tz=bad, now=NOW, window="1h").code == "invalid_query"
        )


def test_relative_window_resolves_once_with_utc_and_local_endpoints():
    win = resolve_window(tz="America/Chicago", now=NOW, window="6h")
    assert win.end == NOW
    assert win.start == datetime(2025, 6, 15, 6, 0, tzinfo=UTC)
    described = win.describe()
    assert "2025-06-15T07:00:00-05:00" in described
    assert "2025-06-15T06:00:00+00:00" in described


def test_window_bounds():
    ok = resolve_window(tz="UTC", now=NOW, window="7d")
    assert ok.span == 7 * 86400
    assert (
        fail(resolve_window, tz="UTC", now=NOW, window=7 * 86400 + 1).code
        == "invalid_query"
    )
    assert (
        "future"
        in fail(
            resolve_window,
            tz="UTC",
            now=NOW,
            start="2025-06-15T11:00:00Z",
            end="2025-06-15T12:00:01Z",
        ).message
    )
    assert (
        "before start"
        in fail(
            resolve_window,
            tz="UTC",
            now=NOW,
            start="2025-06-15T11:00:00Z",
            end="2025-06-15T10:00:00Z",
        ).message
    )


def test_default_step_rounds_up_to_at_most_1000_points():
    for span in ("1h", "6h", "2d", "7d"):
        win = resolve_window(tz="UTC", now=NOW, window=span)
        assert win.step >= 15
        assert win.points <= 1000
        assert win.step_was_default
    assert resolve_window(tz="UTC", now=NOW, window="1h").step == 15


def test_explicit_step_is_never_coarsened():
    assert (
        fail(resolve_window, tz="UTC", now=NOW, window="1h", step="14s").minimum_step
        == 15
    )
    err = fail(resolve_window, tz="UTC", now=NOW, window="7d", step="15s")
    assert err.minimum_step == 303
    assert resolve_window(tz="UTC", now=NOW, window="7d", step="303s").points <= 2000
    # exactly 2000 points passes, 2001 fails and names the minimum
    start = "2025-06-15T00:00:00Z"
    at_limit = resolve_window(
        tz="UTC", now=NOW, start=start, end="2025-06-15T09:59:42Z", step="18s"
    )
    assert at_limit.points == 2000
    over = fail(
        resolve_window,
        tz="UTC",
        now=NOW,
        start=start,
        end="2025-06-15T10:00:00Z",
        step="18s",
    )
    assert over.minimum_step == 19


@pytest.mark.parametrize(
    ("moment", "why"),
    [("2025-03-09T02:30:00", "does not exist"), ("2025-11-02T01:30:00", "ambiguous")],
)
def test_dst_gap_and_fold_are_rejected_unless_disambiguated(moment, why):
    err = fail(
        resolve_window,
        tz="America/New_York",
        now=datetime(2026, 1, 1, tzinfo=UTC),
        start=moment,
        end="2025-12-01T00:00:00Z",
    )
    assert why in err.message


def test_dst_fold_with_explicit_offsets_is_exact():
    first = resolve_window(
        tz="America/New_York",
        now=datetime(2026, 1, 1, tzinfo=UTC),
        start="2025-11-02T01:30:00-04:00",
        end="2025-11-02T01:30:00-05:00",
    )
    assert first.span == 3600
    plain = resolve_window(
        tz="America/New_York",
        now=NOW,
        start="2025-06-01T10:00:00",
        end="2025-06-01T11:00:00",
    )
    assert plain.start == datetime(2025, 6, 1, 14, 0, tzinfo=UTC)
    assert load_tz("UTC").key == "UTC"


# -- normalization -----------------------------------------------------------------


def test_zero_is_a_value_and_empty_is_absent(server):
    server.routes[f"{PROXY}/query"] = success(vector(({"job": "a"}, "0")))
    result, at, zone = prom(server).instant("x", tz="UTC")
    assert not result.absent
    assert result.series[0].points[0][1] == 0.0
    assert "= 0 @" in text.format_query(result, query="x", source="s", tz=zone, at=at)
    server.routes[f"{PROXY}/query"] = success(vector())
    result, at, zone = prom(server).instant("x", tz="UTC")
    assert result.absent
    rendered = text.format_query(result, query="x", source="s", tz=zone, at=at)
    assert "ABSENT" in rendered
    assert "not zero" in rendered


def test_range_requests_carry_resolved_bounds_step_and_timeout(server):
    server.routes[f"{PROXY}/query_range"] = success(matrix(({}, [(1749967200, "1")])))
    _, win = prom(server).range("up", tz="UTC", window="6h")
    query = server.requests[0]["query"]
    assert query["timeout"] == "8s"
    assert float(query["end"]) == NOW.timestamp()
    assert float(query["start"]) == NOW.timestamp() - 6 * 3600
    assert int(query["step"]) == win.step


def test_nan_inf_gaps_and_staleness_are_annotated_not_filled():
    data = matrix(
        ({"a": "1"}, [(0, "1"), (15, "NaN"), (30, "+Inf"), (45, "-Inf"), (120, "0")])
    )
    result = normalize(data, "range", step=15)
    series = result.series[0]
    assert (series.nan, series.pos_inf, series.neg_inf) == (1, 1, 1)
    assert series.gaps == 1
    assert len(series.points) == 5
    assert series.points[-1] == (120.0, 0.0)
    rendered = text.format_query(result, query="q", source="s", tz=load_tz("UTC"))
    assert "NaN=1 +Inf=1 -Inf=1" in rendered
    assert "missing steps are not zero" in rendered


def test_native_histograms_are_unsupported_not_flattened():
    data = {
        "resultType": "vector",
        "result": [
            {
                "metric": {"a": "1"},
                "histogram": [1, {"count": "3", "sum": "5", "buckets": []}],
            }
        ],
    }
    result = normalize(data, "instant")
    assert result.histogram_series == 1
    assert result.series == []
    assert not result.absent
    assert result.histograms_only
    assert "UNSUPPORTED" in text.format_query(
        result, query="q", source="s", tz=load_tz("UTC")
    )


def test_series_limit_is_exact():
    ok = vector(*[({"i": str(i)}, "1") for i in range(MAX_SERIES)])
    assert len(normalize(ok, "instant").series) == MAX_SERIES
    over = vector(*[({"i": str(i)}, "1") for i in range(MAX_SERIES + 1)])
    err = fail(normalize, over, "instant")
    assert err.code == "bounds_exceeded"
    assert "aggregate" in err.message


def test_sample_limit_is_exact():
    def shaped(extra):
        rows = [
            ({"i": str(i)}, [(t, "1") for t in range(1000)])
            for i in range(MAX_SERIES - 1)
        ]
        rows.append(
            ({"i": "last"}, [(t, "1") for t in range(MAX_SAMPLES - 99_000 + extra)])
        )
        return matrix(*rows)

    assert normalize(shaped(0), "range").total_samples == MAX_SAMPLES
    assert fail(normalize, shaped(1), "range").code == "bounds_exceeded"


def test_expression_size_limit(server):
    server.routes[f"{PROXY}/query"] = success(vector(({}, "1")))
    client = prom(server)
    client.instant("a" * 8192, tz="UTC")
    n = len(server.requests)
    assert fail(client.instant, "a" * 8193, tz="UTC").code == "invalid_query"
    assert fail(client.instant, "  ", tz="UTC").code == "invalid_query"
    assert len(server.requests) == n


def test_instant_rejects_future_time(server):
    assert (
        fail(prom(server).instant, "up", tz="UTC", at="2025-06-15T12:00:01Z").code
        == "invalid_query"
    )


# -- targets -----------------------------------------------------------------------

TARGETS = {
    "activeTargets": [
        {
            "labels": {"job": "api", "instance": "10.0.0.1:80", "namespace": "prod"},
            "health": "up",
            "lastScrape": "2025-06-15T11:59:50Z",
            "lastError": "",
            "scrapeUrl": "http://x/metrics?token=hide",
        },
        {
            "labels": {"job": "api", "instance": "10.0.0.2:80", "namespace": "prod"},
            "health": "down",
            "lastScrape": "2025-06-15T11:59:51Z",
            "lastError": "connection refused",
        },
        {
            "labels": {"job": "db", "instance": "10.0.0.3:5432", "namespace": "data"},
            "health": "up",
            "lastScrape": "2025-06-15T11:59:52Z",
            "lastError": "",
        },
    ]
}


def test_target_health_is_separate_from_up_values_and_not_app_health(server):
    server.routes[f"{PROXY}/targets"] = success(TARGETS)
    client = prom(server)
    info = client.targets(job="api")
    assert info["counts"] == {"up": 1, "down": 1}
    assert "scrapeUrl" not in json.dumps(info)
    assert "hide" not in json.dumps(info)
    rendered = text.format_targets(info, source="s", job="api", namespace=None)
    assert "connection refused" in rendered
    assert "does not prove the application is healthy" in rendered
    assert "Separate: `up`" not in rendered
    server.routes[f"{PROXY}/query"] = success(vector(({"job": "api"}, "0")))
    result, at, zone = client.instant(client.up_selector("api", None), tz="UTC")
    both = text.format_targets(
        info,
        source="s",
        job="api",
        namespace=None,
        up=text.format_query(result, query="up", source="s", tz=zone, at=at),
    )
    assert "Separate: `up`" in both
    assert server.requests[-1]["query"]["query"] == 'up{job="api"}'


def test_missing_target_is_a_gap_and_selector_is_escaped(server):
    server.routes[f"{PROXY}/targets"] = success(TARGETS)
    client = prom(server)
    info = client.targets(job="ghost")
    assert info["total"] == 0
    assert "NO MATCHING TARGETS" in text.format_targets(
        info, source="s", job="ghost", namespace=None
    )
    assert client.up_selector('a"b\\', None) == 'up{job="a\\"b\\\\"}'


# -- grafana -----------------------------------------------------------------------


def test_datasource_listing_is_sanitized_and_allowlisted(server):
    server.routes["/api/datasources"] = Reply(
        200,
        [
            {
                "name": "Prom",
                "uid": "prom",
                "type": "prometheus",
                "isDefault": True,
                "url": "http://internal:9090",
                "secureJsonData": {"password": "hunter2"},
                "basicAuthUser": "admin",
                "jsonData": {"x": 1},
            },
            {
                "name": "PG",
                "uid": "pg",
                "type": "postgres",
                "isDefault": False,
                "url": "db:5432",
            },
        ],
    )
    rows = GrafanaClient(profile(server), token=TOKEN).datasources()
    assert rows == [
        {"name": "Prom", "uid": "prom", "type": "prometheus", "default": True}
    ]


def test_dashboard_summary_and_ownership(server):
    server.routes["/api/dashboards/uid/octo-x"] = Reply(
        200,
        {
            "meta": {
                "provisioned": True,
                "provisionedExternalId": "x.json",
                "folderTitle": "Ops",
                "slug": "x",
            },
            "dashboard": {
                "title": "X",
                "tags": ["a"],
                "version": 3,
                "templating": {
                    "list": [
                        {
                            "name": "ns",
                            "type": "query",
                            "query": "label_values(ns)",
                            "datasource": {"uid": "prom"},
                        }
                    ]
                },
                "panels": [
                    {
                        "id": 1,
                        "title": "Row",
                        "type": "row",
                        "panels": [
                            {
                                "id": 2,
                                "title": "CPU",
                                "type": "timeseries",
                                "datasource": {"uid": "prom"},
                                "targets": [
                                    {
                                        "refId": "A",
                                        "expr": "rate(cpu[$__rate_interval])",
                                    }
                                ],
                            }
                        ],
                    },
                    {
                        "id": 3,
                        "title": "SQL",
                        "type": "table",
                        "targets": [
                            {
                                "refId": "A",
                                "rawSql": "select 1",
                                "datasource": {"uid": "pg"},
                            }
                        ],
                    },
                ],
            },
        },
    )
    server.routes["/api/dashboards/uid/other"] = Reply(
        200, {"meta": {"provisioned": False}, "dashboard": {"title": "Y", "panels": []}}
    )
    prof = profile(
        server,
        ownership=[
            {
                "match_uid_prefix": "octo-",
                "repo": "org/infra",
                "path_glob": "dash/*.yaml",
            }
        ],
    )
    client = GrafanaClient(prof, token=TOKEN)
    summary = client.dashboard("octo-x")
    assert [p["id"] for p in summary["panels"]] == [1, 2, 3]
    assert summary["datasource_uids"] == ["pg", "prom"]
    assert summary["variables"][0]["name"] == "ns"
    assert summary["provisioned"] is True
    assert summary["ownership"]["owner"] == {
        "repo": "org/infra",
        "path_glob": "dash/*.yaml",
    }
    unknown = client.dashboard("other")
    assert unknown["provisioned"] is False
    assert unknown["ownership"]["owner"] == "unknown"
    assert "no git owner" in unknown["ownership"]["how_to_change"]
    assert "unknown" in text.format_dashboard(unknown)


def test_dashboard_search_builds_links_on_the_public_origin(server):
    server.routes["/api/search"] = Reply(
        200,
        [
            {
                "uid": "abc",
                "title": "T",
                "tags": ["x"],
                "folderTitle": "F",
                "url": "http://evil/d/abc",
            },
            {"uid": "bad uid!", "title": "skip"},
        ],
    )
    found = GrafanaClient(profile(server), token=TOKEN).search_dashboards(
        "t", limit=500
    )
    assert [i["uid"] for i in found["items"]] == ["abc"]
    assert found["items"][0]["link"] == "https://grafana.example.com/d/abc"
    assert server.requests[0]["query"]["limit"] == "100"


def test_grafana_needs_a_token(server):
    assert fail(GrafanaClient, profile(server), token=None).code == "not_configured"


def test_deep_links_are_encoded(server):
    win = resolve_window(tz="Europe/Berlin", now=NOW, window="1h")
    url = dashboard_link(
        profile(server),
        "uid-1",
        panel_id=7,
        start=win.start,
        end=win.end,
        tz=win.tz,
        variables={"ns": "a b&c=d", "pod": ["x/y", "ü"]},
    )
    parts = urlsplit(url)
    assert (
        f"{parts.scheme}://{parts.netloc}{parts.path}"
        == "https://grafana.example.com/d/uid-1"
    )
    query = parse_qs(parts.query)
    assert query["viewPanel"] == ["7"]
    assert query["timezone"] == ["Europe/Berlin"]
    assert query["to"] == [str(int(NOW.timestamp() * 1000))]
    assert query["var-ns"] == ["a b&c=d"]
    assert query["var-pod"] == ["x/y", "ü"]
    assert "&c=d" not in parts.query
    for bad in ("../x", "a b", "a/b", ""):
        assert fail(dashboard_link, profile(server), bad).code == "invalid_query"
    assert (
        fail(dashboard_link, profile(server), "u", variables={"bad name": "x"}).code
        == "invalid_query"
    )
    assert (
        fail(dashboard_link, profile(server), "u", panel_id=True).code
        == "invalid_query"
    )


# -- renderer and charts -----------------------------------------------------------


def fake_png(width, height):
    return (
        b"\x89PNG\r\n\x1a\n"
        + struct.pack(">I", 13)
        + b"IHDR"
        + struct.pack(">II", width, height)
        + b"\x08\x06\x00\x00\x00"
    )


def test_renderer_error_png_is_not_success():
    placeholder = Response(200, "image/png", fake_png(478, 208))
    err = fail(charts.verify_render, placeholder, 1000, 500)
    assert err.code == "upstream_unavailable"
    assert "478x208" in err.message
    assert (
        fail(
            charts.verify_render, Response(200, "text/html", b"<html>"), 1000, 500
        ).code
        == "upstream_unavailable"
    )
    assert (
        fail(
            charts.verify_render, Response(200, "image/png", b"not a png"), 1000, 500
        ).code
        == "upstream_unavailable"
    )
    real = Response(200, "image/png", fake_png(1000, 500))
    assert charts.verify_render(real, 1000, 500) == real.body


def test_renderer_500_html_is_an_error_not_an_image(server):
    server.routes["/render/d-solo/uid/slug"] = Reply(
        500, b"<html>boom</html>", ctype="text/html"
    )
    client = GrafanaClient(profile(server, renderer=True), token=TOKEN)
    win = resolve_window(tz="UTC", now=NOW, window="1h")
    err = fail(
        client.render_panel,
        "uid",
        "slug",
        1,
        start=win.start,
        end=win.end,
        tz=win.tz,
        width=1000,
        height=500,
    )
    assert err.code == "upstream_unavailable"
    assert "boom" not in str(err)


def chart_inputs(server):
    server.routes[f"{PROXY}/query_range"] = success(
        matrix(
            (
                {"__name__": "cpu", "job": "a"},
                [(1749967200 + i * 60, str(i % 5)) for i in range(60)],
            ),
            (
                {"__name__": "cpu", "job": "b"},
                [(1749967200 + i * 60, "0") for i in range(30)],
            ),
        )
    )
    result, win = prom(server).range("cpu", tz="UTC", window="1h", step="60s")
    return result, charts.ChartMeta("cpu", "profile=test", win)


def test_chart_png_is_within_bounds_and_labeled(server, monkeypatch):
    pytest.importorskip("matplotlib")
    from matplotlib.figure import Figure

    texts = []
    original = Figure.text

    def spy(self, x, y, s, *args, **kwargs):
        texts.append(s)
        return original(self, x, y, s, *args, **kwargs)

    monkeypatch.setattr(Figure, "text", spy)
    result, meta = chart_inputs(server)
    png = charts.render_chart(result, meta)
    assert charts.png_size(png) == (1200, 675)
    assert len(png) <= charts.MAX_PNG_BYTES
    footer = "\n".join(texts)
    assert "Data chart, not a Grafana panel screenshot" in footer
    for needle in ("query: cpu", "tz UTC", "step 60s", "profile=test"):
        assert needle in footer
    assert charts.png_size(
        charts.render_chart(result, meta, width=1600, height=900)
    ) == (1600, 900)
    assert (
        fail(charts.render_chart, result, meta, width=1601, height=900).code
        == "invalid_query"
    )
    assert (
        fail(charts.render_chart, result, meta, width=1600, height=901).code
        == "invalid_query"
    )


def test_chart_refuses_absent_instant_and_histogram_data(server):
    pytest.importorskip("matplotlib")
    _, meta = chart_inputs(server)
    absent = normalize(matrix(), "range", 60)
    assert "absent" in fail(charts.render_chart, absent, meta).message
    instant = normalize(vector(({}, "1")), "instant")
    assert fail(charts.render_chart, instant, meta).code == "invalid_query"
    hist = normalize(
        {"resultType": "matrix", "result": [{"metric": {}, "histograms": [[1, {}]]}]},
        "range",
        60,
    )
    assert "histograms" in fail(charts.render_chart, hist, meta).message


def test_chart_draws_zero_series_and_gaps(server):
    pytest.importorskip("matplotlib")
    _, meta = chart_inputs(server)
    data = matrix(
        ({"a": "1"}, [(1749967200, "0"), (1749967260, "0"), (1749968000, "0")])
    )
    png = charts.render_chart(normalize(data, "range", 60), meta)
    assert charts.png_size(png) == (1200, 675)
