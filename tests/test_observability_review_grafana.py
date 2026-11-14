"""Regression coverage for Grafana authorization and bounded summaries."""

import io
from datetime import UTC
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from PIL import Image
from PIL import PngImagePlugin

from django_ergo.bots.tools import ToolContext
from django_ergo.conversation.images import ToolResult
from django_ergo.observability import grafana
from django_ergo.observability import text
from django_ergo.observability.errors import ObservabilityError
from django_ergo.observability.grafana import GrafanaClient
from django_ergo.observability.grafana import dashboard_link
from django_ergo.observability.grafana import locate
from django_ergo.observability.profile import Profile
from django_ergo.plugins.observability import ObservabilityPlugin
from tests.observability_server import FakeServer
from tests.observability_server import Reply

TOKEN = "FAKE_REVIEW_TOKEN"
DS = {"uid": "allowed", "type": "prometheus"}
NOW = datetime(2025, 1, 1, tzinfo=UTC)


@pytest.fixture
def server():
    srv = FakeServer()
    yield srv
    srv.close()


def profile(server, **kwargs):
    return Profile("test", grafana_url=server.url, datasources=("allowed",), **kwargs)


def render(client, panel_id=1):
    return client.render_panel(
        "u", panel_id, start=NOW, end=NOW, tz=ZoneInfo("UTC"), width=1000, height=500
    )


def png(secret=None):
    stream = io.BytesIO()
    info = PngImagePlugin.PngInfo()
    if secret:
        info.add_text("note", secret)
    Image.new("RGB", (1000, 500)).save(stream, format="PNG", pnginfo=info)
    return stream.getvalue()


def dashboard(server, panels, **extra):
    server.routes["/api/dashboards/uid/u"] = Reply(
        200, {"dashboard": {"panels": panels, **extra}, "meta": {"slug": "slug"}}
    )
    server.routes["/render/d-solo/u/slug"] = Reply(200, png(), ctype="image/png")


@pytest.mark.parametrize(
    "panel",
    [
        {"id": 1, "datasource": {"uid": "forbidden", "type": "postgres"}},
        {"id": 1, "datasource": {"uid": "${source}", "type": "prometheus"}},
        {
            "id": 1,
            "datasource": {"uid": "allowed", "type": "mixed"},
            "targets": [{"datasource": DS}],
        },
        {"id": 1, "datasource": DS, "targets": [{"datasource": {"uid": "forbidden"}}]},
        {"id": 1, "datasource": DS, "targets": [{"datasource": None}]},
        {"id": 1, "datasource": DS, "libraryPanel": {"uid": "library"}},
        {"id": 1, "targets": [{}]},
        {"id": 1, "datasource": "allowed"},
        {
            "id": 1,
            "datasource": DS,
            "transformations": [{"datasourceUid": "forbidden"}],
        },
    ],
)
def test_renderer_denies_unresolved_or_unauthorized(server, panel):
    dashboard(server, [panel])
    with pytest.raises(ObservabilityError) as exc:
        render(GrafanaClient(profile(server), token=TOKEN))
    assert exc.value.code == "forbidden"
    assert [r["path"] for r in server.requests] == ["/api/dashboards/uid/u"]


def test_renderer_uses_complete_dashboard_not_summary(server):
    panels = [{"id": i + 2, "datasource": DS} for i in range(200)]
    panels.append(
        {
            "id": 1,
            "datasource": DS,
            "targets": [{}] * 20 + [{"datasource": {"uid": "forbidden"}}],
        }
    )
    dashboard(server, panels)
    with pytest.raises(ObservabilityError):
        render(GrafanaClient(profile(server), token=TOKEN))
    assert len(server.requests) == 1


@pytest.mark.parametrize(
    "extra",
    [
        {"templating": {"list": [{"name": "source", "type": "datasource"}]}},
        {"annotations": {"list": [{"datasource": {"uid": "forbidden"}}]}},
    ],
)
def test_dashboard_level_queries_fail_closed(server, extra):
    dashboard(server, [{"id": 1, "datasource": DS}], **extra)
    with pytest.raises(ObservabilityError) as exc:
        render(GrafanaClient(profile(server), token=TOKEN))
    assert exc.value.code == "forbidden"
    assert len(server.requests) == 1


def test_allowed_renderer_tool_and_png_secret_rejection(server, monkeypatch):
    dashboard(server, [{"id": 1, "datasource": DS, "targets": [{}]}])
    monkeypatch.setenv("GRAFANA_REVIEW_TEST_TOKEN", TOKEN)
    plugin = ObservabilityPlugin(
        None,
        {
            "profiles": {
                "main": {
                    "grafana_url": server.url,
                    "token_env": "GRAFANA_REVIEW_TEST_TOKEN",
                    "datasources": ["allowed"],
                    "renderer": True,
                }
            }
        },
    )
    toolkit = plugin.toolkits(ToolContext())[0]
    args = {"uid": "u", "panel_id": 1, "tz": "UTC", "window": "1h"}
    result = toolkit.execute_tool("ergo_grafana_panel_render", args)
    assert isinstance(result, ToolResult)
    assert server.requests[-1]["path"] == "/render/d-solo/u/slug"
    server.routes["/render/d-solo/u/slug"] = Reply(200, png(TOKEN), ctype="image/png")
    result = toolkit.execute_tool("ergo_grafana_panel_render", args)
    assert isinstance(result, str) and result.startswith("ERROR [upstream_unavailable]")
    assert TOKEN not in result


def test_link_cardinality_and_encoded_size_boundaries(server, monkeypatch):
    prof = profile(server)
    assert dashboard_link(prof, "u", variables={"x": ["a"] * grafana.MAX_VAR_VALUES})
    with pytest.raises(ObservabilityError) as exc:
        dashboard_link(prof, "u", variables={"x": ["a"] * (grafana.MAX_VAR_VALUES + 1)})
    assert exc.value.code == "bounds_exceeded"
    url = dashboard_link(prof, "u", variables={"x": "ü /"})
    monkeypatch.setattr(grafana, "MAX_LINK_BYTES", len(url.encode()))
    assert dashboard_link(prof, "u", variables={"x": "ü /"}) == url
    monkeypatch.setattr(grafana, "MAX_LINK_BYTES", len(url.encode()) - 1)
    with pytest.raises(ObservabilityError) as exc:
        dashboard_link(prof, "u", variables={"x": "ü /"})
    assert exc.value.code == "bounds_exceeded"


def test_inventory_full_and_summary_truncation_marked(server):
    panels = [{"id": i} for i in range(200)] + [
        {"id": 201, "datasource": {"uid": "last"}}
    ]
    panels[0]["targets"] = [{"expr": "x" * 501}] * 20 + [
        {"datasource": {"uid": "target21"}}
    ]
    dashboard(server, panels)
    summary = GrafanaClient(profile(server), token=TOKEN).dashboard("u")
    assert summary["datasource_uids"] == ["last", "target21"]
    assert summary["panels_truncated"] and summary["panels"][0]["targets_truncated"]
    formatted = text.format_dashboard(summary)
    assert "panel summaries truncated" in formatted
    assert "target summaries truncated" in formatted
    assert "[clipped]" in formatted


@pytest.mark.parametrize("provisioned", [True, False, None])
@pytest.mark.parametrize("owner", [False, True])
def test_ownership_independent_of_provisioning(server, provisioned, owner):
    rules = (
        ({"match_uid": "u", "repo": "example/infra", "path_glob": "dashboard.json"},)
        if owner
        else ()
    )
    result = locate(
        profile(server, ownership=rules),
        {"uid": "u", "provisioned": provisioned, "provisioned_file": ""},
    )
    assert result["provisioned"] is provisioned
    assert (result["owner"] != "unknown") == owner
    assert ("configured ownership mapping" in result["how_to_change"]) == owner
    assert ("ownership unknown" in result["how_to_change"]) != owner


@pytest.mark.parametrize(
    ("path", "body", "method"),
    [
        ("/api/search", [None], "search_dashboards"),
        ("/api/search", [{"uid": "u", "tags": 1}], "search_dashboards"),
        ("/api/datasources", [None], "datasources"),
        ("/api/dashboards/uid/u", {"dashboard": {}, "meta": []}, "dashboard"),
        (
            "/api/dashboards/uid/u",
            {"dashboard": {"templating": {"list": [None]}}},
            "dashboard",
        ),
        (
            "/api/dashboards/uid/u",
            {"dashboard": {"panels": [{"targets": [None]}]}},
            "dashboard",
        ),
        (
            "/api/dashboards/uid/u",
            {"dashboard": {"panels": [{"panels": {}}]}},
            "dashboard",
        ),
    ],
)
def test_malformed_nested_data_is_typed(server, path, body, method):
    server.routes[path] = Reply(200, body)
    client = GrafanaClient(profile(server), token=TOKEN)
    with pytest.raises(ObservabilityError) as exc:
        getattr(client, method)(*(("u",) if method == "dashboard" else ()))
    assert exc.value.code == "upstream_unavailable"


@pytest.mark.parametrize("depth", [600, 1100])
def test_deep_dashboard_json_is_typed(server, depth):
    body = b'{"dashboard":{"unused":' + b"[" * depth + b"0" + b"]" * depth + b"}}"
    server.routes["/api/dashboards/uid/u"] = Reply(200, body)
    with pytest.raises(ObservabilityError) as exc:
        GrafanaClient(profile(server), token=TOKEN).dashboard("u")
    assert exc.value.code == "upstream_unavailable"


@pytest.mark.parametrize(
    "count", [grafana.MAX_DATASOURCES, grafana.MAX_DATASOURCES + 1]
)
def test_datasource_inventory_limit(server, count):
    server.routes["/api/datasources"] = Reply(200, [DS] * count)
    client = GrafanaClient(profile(server), token=TOKEN)
    if count == grafana.MAX_DATASOURCES:
        assert len(client.datasources()) == count
    else:
        with pytest.raises(ObservabilityError) as exc:
            client.datasources()
        assert exc.value.code == "bounds_exceeded"


def test_upstream_text_redacted_before_clipping(server):
    dashboard(
        server,
        [
            {
                "id": 1,
                "title": "x" * 195 + TOKEN,
                "datasource": DS,
                "targets": [{"expr": "x" * 495 + TOKEN}],
            }
        ],
        title=TOKEN,
        tags=[TOKEN],
        templating={"list": [{"name": TOKEN, "query": TOKEN}]},
    )
    client = GrafanaClient(profile(server), token=TOKEN)
    summary = client.dashboard("u")
    assert TOKEN not in str(summary)
    assert TOKEN not in text.format_dashboard(summary)
    server.routes["/api/search"] = Reply(
        200, [{"uid": "u", "title": TOKEN, "tags": [TOKEN]}]
    )
    assert TOKEN not in str(client.search_dashboards())
    server.routes["/api/datasources"] = Reply(
        200, [{"uid": "allowed", "name": TOKEN, "type": TOKEN}]
    )
    assert TOKEN not in str(client.datasources())
