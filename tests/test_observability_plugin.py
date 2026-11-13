"""The observability plugin: host-owned config, tool surface and real chart attachments."""

from __future__ import annotations

import json
from datetime import UTC
from datetime import datetime

import pytest
from django.contrib.auth import get_user_model

from django_ergo.bots.plugins import resolve_plugin_class
from django_ergo.bots.tools import ToolContext
from django_ergo.conversation.images import ToolResult
from django_ergo.conversation.models import ConversationAttachment
from django_ergo.conversation.models import ConversationSession
from django_ergo.observability import charts
from django_ergo.plugins.observability import ObservabilityPlugin
from tests.observability_server import FakeServer
from tests.observability_server import Reply
from tests.observability_server import matrix
from tests.observability_server import success
from tests.observability_server import vector

pytestmark = pytest.mark.django_db

NOW = datetime(2025, 6, 15, 12, 0, 0, tzinfo=UTC)
TOKEN = "glsa_PLUGIN_SECRET"
PROXY = "/api/datasources/proxy/uid/prom/api/v1"


class FixedClockPlugin(ObservabilityPlugin):
    clock = staticmethod(lambda: NOW)


@pytest.fixture
def server():
    srv = FakeServer()
    yield srv
    srv.close()


@pytest.fixture(autouse=True)
def media_root(settings, tmp_path):
    settings.MEDIA_ROOT = str(tmp_path)


@pytest.fixture
def token(monkeypatch):
    monkeypatch.setenv("OBS_TEST_TOKEN", TOKEN)


def make_plugin(server, **profile):
    cfg = {
        "grafana_url": server.url,
        "public_url": "https://grafana.example.com",
        "token_env": "OBS_TEST_TOKEN",
        "datasources": ["prom"],
        **profile,
    }
    return FixedClockPlugin(None, {"profiles": {"main": cfg}})


def call(plugin, ctx, tool, **args):
    toolkit = plugin.toolkits(ctx)[0]
    return toolkit.execute_tool(tool, args)


@pytest.fixture
def session():
    user = get_user_model().objects.create_user(username="obs", password="x")
    return ConversationSession.objects.create(
        user=user, engine_type="claude", transport_type="api", status="active"
    )


def test_registered_as_an_official_plugin():
    assert resolve_plugin_class("observability") is ObservabilityPlugin


@pytest.mark.parametrize(
    "bad",
    [
        {},
        {"profiles": {}},
        {"profiles": {"a": {"grafana_url": "ftp://x", "datasources": ["p"]}}},
        {"profiles": {"a": {"grafana_url": "http://u:p@x", "datasources": ["p"]}}},
        {"profiles": {"a": {"grafana_url": "http://x", "datasources": ["p/../q"]}}},
        {"profiles": {"a": {"grafana_url": "http://x", "headers": {"X": "1"}}}},
        {"profiles": {"a": {"mode": "direct", "datasources": ["p"]}}},
        {"profiles": {"a": {"grafana_url": "http://x"}}, "url": "http://evil"},
        {"profiles": {"a": {"grafana_url": "http://x", "ownership": [{"repo": "r"}]}}},
    ],
)
def test_bad_config_is_rejected_at_load(bad):
    with pytest.raises(ValueError, match="observability"):
        ObservabilityPlugin(None, bad)


def test_tool_surface_and_renderer_is_opt_in(server):
    names = lambda plugin: set(plugin.toolkits(ToolContext())[0].tools)  # noqa: E731
    base = {
        "ergo_grafana_search",
        "ergo_grafana_dashboard",
        "ergo_prom_query",
        "ergo_prom_targets",
        "ergo_grafana_link",
        "ergo_prom_chart",
    }
    assert names(make_plugin(server)) == base
    assert names(make_plugin(server, renderer=True)) == base | {
        "ergo_grafana_panel_render"
    }


def test_models_cannot_supply_urls_headers_or_credentials(server, token):
    plugin = make_plugin(server)
    ctx = ToolContext()
    for arg in ("url", "headers", "token", "token_env", "grafana_url"):
        with pytest.raises(TypeError):
            call(plugin, ctx, "ergo_prom_query", expr="up", tz="UTC", **{arg: "x"})
    assert server.requests == []


def test_non_allowlisted_datasource_fails_closed(server, token):
    out = call(
        make_plugin(server),
        ToolContext(),
        "ergo_prom_query",
        expr="up",
        tz="UTC",
        datasource="pg",
    )
    assert out.startswith("ERROR [not_configured]")
    assert server.requests == []


def test_token_is_read_per_call_from_env_or_file_and_never_echoed(
    server, monkeypatch, tmp_path
):
    server.routes[f"{PROXY}/query"] = success(vector(({"job": "a"}, "0")))
    plugin = make_plugin(server)
    ctx = ToolContext()
    assert "no Grafana token" in call(
        plugin, ctx, "ergo_prom_query", expr="up", tz="UTC"
    )
    assert server.requests == []
    monkeypatch.setenv("OBS_TEST_TOKEN", "first")
    call(plugin, ctx, "ergo_prom_query", expr="up", tz="UTC")
    monkeypatch.setenv("OBS_TEST_TOKEN", "second")
    call(plugin, ctx, "ergo_prom_query", expr="up", tz="UTC")
    assert [r["headers"]["Authorization"] for r in server.requests] == [
        "Bearer first",
        "Bearer second",
    ]
    monkeypatch.delenv("OBS_TEST_TOKEN")
    token_file = tmp_path / "token"
    token_file.write_text("from-file\n")
    file_plugin = make_plugin(server, token_file=str(token_file))
    call(file_plugin, ctx, "ergo_prom_query", expr="up", tz="UTC")
    token_file.write_text("rotated\n")
    call(file_plugin, ctx, "ergo_prom_query", expr="up", tz="UTC")
    assert [r["headers"]["Authorization"] for r in server.requests[2:]] == [
        "Bearer from-file",
        "Bearer rotated",
    ]


def test_auth_failures_are_typed_and_leak_nothing(server, token):
    for status, code in ((401, "unauthorized"), (403, "forbidden")):
        server.routes[f"{PROXY}/query"] = Reply(status, {"message": f"token {TOKEN}"})
        out = call(
            make_plugin(server), ToolContext(), "ergo_prom_query", expr="up", tz="UTC"
        )
        assert out.startswith(f"ERROR [{code}]")
        assert TOKEN not in out


def test_query_text_carries_provenance_range_and_link(server, token):
    server.routes[f"{PROXY}/query_range"] = success(
        matrix(({"job": "a"}, [(1749967200, "0"), (1749967260, "2")]))
    )
    out = call(
        make_plugin(server),
        ToolContext(),
        "ergo_prom_query",
        expr="up",
        tz="America/Chicago",
        mode="range",
        window="6h",
    )
    assert "profile=main grafana datasource proxy datasource=prom" in out
    assert "local (America/Chicago)" in out
    assert "UTC 2025-06-15T06:00:00+00:00" in out
    assert "https://grafana.example.com/explore?" in out
    assert "min=0 max=2" in out


def test_explicit_step_over_limit_returns_required_minimum(server, token):
    out = call(
        make_plugin(server),
        ToolContext(),
        "ergo_prom_query",
        expr="up",
        tz="UTC",
        mode="range",
        window="7d",
        step="15s",
    )
    assert out.startswith("ERROR [invalid_query]")
    assert "303s" in out
    assert server.requests == []


def test_target_health_tool_separates_scrape_from_up(server, token):
    server.routes[f"{PROXY}/targets"] = success(
        {
            "activeTargets": [
                {
                    "labels": {"job": "api"},
                    "health": "up",
                    "lastScrape": "t",
                    "lastError": "",
                }
            ]
        }
    )
    server.routes[f"{PROXY}/query"] = success(vector(({"job": "api"}, "1")))
    out = call(
        make_plugin(server),
        ToolContext(),
        "ergo_prom_targets",
        job="api",
        include_up=True,
    )
    assert "does not prove the application is healthy" in out
    assert "Separate: `up`" in out


def test_link_tool_encodes_and_resolves_the_window(server, token):
    out = call(
        make_plugin(server),
        ToolContext(),
        "ergo_grafana_link",
        uid="abc",
        panel_id=4,
        tz="UTC",
        window="1h",
        variables={"ns": "a&b"},
    )
    assert "https://grafana.example.com/d/abc?viewPanel=4" in out
    assert "var-ns=a%26b" in out
    assert "timezone=UTC" in out


def test_dashboard_tool_reports_provisioning_and_unknown_owner(server, token):
    server.routes["/api/dashboards/uid/abc"] = Reply(
        200,
        {
            "meta": {"provisioned": True, "provisionedExternalId": "abc.json"},
            "dashboard": {"title": "T", "panels": []},
        },
    )
    out = call(make_plugin(server), ToolContext(), "ergo_grafana_dashboard", uid="abc")
    assert "provisioned: True" in out
    assert "owner: unknown" in out


def test_chart_tool_creates_a_real_attachment_with_metadata(server, token, session):
    pytest.importorskip("matplotlib")
    server.routes[f"{PROXY}/query_range"] = success(
        matrix(
            (
                {"__name__": "cpu"},
                [(1749967200 + i * 60, str(i % 3)) for i in range(60)],
            )
        )
    )
    result = call(
        make_plugin(server),
        ToolContext(session=session),
        "ergo_prom_chart",
        expr="cpu",
        tz="UTC",
        window="1h",
        step="60s",
    )
    assert isinstance(result, ToolResult)
    assert "Data chart, not a Grafana panel screenshot" in result.text
    row = ConversationAttachment.objects.get(session=session)
    assert row.source == "bot"
    assert row.media_type == "image/png"
    assert row.metadata["query"] == "cpu"
    assert row.metadata["tz"] == "UTC"
    assert row.metadata["step"] == 60
    assert row.metadata["profile"] == "main"
    assert row.metadata["datasource"] == "prom"
    assert row.metadata["range_utc"] == [
        "2025-06-15T11:00:00+00:00",
        "2025-06-15T12:00:00+00:00",
    ]
    assert row.metadata["link"].startswith("https://grafana.example.com/explore?")
    assert [image.attachment_id for image in result.images] == [str(row.id)]
    assert str(row.id) in result.text
    png = row.file.read()
    width, height = charts.png_size(png)
    assert (width, height) == (1200, 675)
    assert len(png) <= charts.MAX_PNG_BYTES
    json.dumps(row.metadata)


def test_chart_of_absent_data_is_an_error_and_stores_nothing(server, token, session):
    pytest.importorskip("matplotlib")
    server.routes[f"{PROXY}/query_range"] = success(matrix())
    out = call(
        make_plugin(server),
        ToolContext(session=session),
        "ergo_prom_chart",
        expr="nothing",
        tz="UTC",
        window="1h",
    )
    assert out.startswith("ERROR [invalid_query]")
    assert "absent" in out
    assert not ConversationAttachment.objects.exists()


def test_chart_without_a_session_still_returns_the_image(server, token):
    pytest.importorskip("matplotlib")
    server.routes[f"{PROXY}/query_range"] = success(
        matrix(({}, [(1749967200, "1"), (1749967260, "1")]))
    )
    result = call(
        make_plugin(server),
        ToolContext(),
        "ergo_prom_chart",
        expr="x",
        tz="UTC",
        window="1h",
    )
    assert charts.png_size(result.images[0].data) == (1200, 675)


def test_panel_render_rejects_the_no_renderer_placeholder(server, token, session):
    import struct

    placeholder = (
        b"\x89PNG\r\n\x1a\n"
        + struct.pack(">I", 13)
        + b"IHDR"
        + struct.pack(">II", 478, 208)
        + b"\x08\x06\x00\x00\x00"
    )
    server.routes["/api/dashboards/uid/abc"] = Reply(
        200,
        {
            "meta": {"provisioned": True, "slug": "slug"},
            "dashboard": {
                "title": "T",
                "panels": [
                    {"id": 1, "datasource": {"uid": "prom", "type": "prometheus"}}
                ],
            },
        },
    )
    server.routes["/render/d-solo/abc/slug"] = Reply(
        200, placeholder, ctype="image/png"
    )
    out = call(
        make_plugin(server, renderer=True),
        ToolContext(session=session),
        "ergo_grafana_panel_render",
        uid="abc",
        panel_id=1,
        tz="UTC",
        window="1h",
    )
    assert out.startswith("ERROR [upstream_unavailable]")
    assert "478x208" in out
    assert not ConversationAttachment.objects.exists()
