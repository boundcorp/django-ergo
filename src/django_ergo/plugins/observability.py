"""Read-only Grafana and Prometheus: dashboards, bounded PromQL, target health, charts.

``bot.yaml``::

    plugins:
      - name: observability
        root_only: false              # optional; default false
        profiles:
          prod:
            grafana_url: http://grafana.monitoring.svc    # fixed origin, host-owned
            public_url: https://grafana.example.com       # origin used in links
            token_env: GRAFANA_VIEWER_TOKEN               # read per call via ctx.secret()
            token_file: /run/secrets/grafana-token        # optional; read per call
            datasources: [prometheus]                     # uids the model may query
            mode: proxy                                   # or direct (+ prometheus_url)
            renderer: false                               # true only with a working renderer
            ownership:                                    # optional, never guessed
              - {match_uid_prefix: "team-", repo: org/infra, path_glob: "dashboards/*.yaml"}

The model never supplies a URL, header, credential name or datasource uid outside
``datasources``. All Grafana access is GET. Tool names start with ``ergo_``.
"""

from __future__ import annotations

import functools
from datetime import UTC
from datetime import datetime

from django_ergo.bots.plugins import BotPlugin
from django_ergo.bots.tools import FunctionToolkit
from django_ergo.bots.tools import bot_tool
from django_ergo.conversation.attachments import save_session_file
from django_ergo.conversation.images import ToolImage
from django_ergo.conversation.images import ToolResult
from django_ergo.observability import charts
from django_ergo.observability import text
from django_ergo.observability.errors import BOUNDS_EXCEEDED
from django_ergo.observability.errors import INVALID_QUERY
from django_ergo.observability.errors import NOT_CONFIGURED
from django_ergo.observability.errors import ObservabilityError
from django_ergo.observability.grafana import GrafanaClient
from django_ergo.observability.grafana import dashboard_link
from django_ergo.observability.grafana import explore_link
from django_ergo.observability.profile import Profile
from django_ergo.observability.prometheus import PromClient
from django_ergo.observability.timeutil import load_tz
from django_ergo.observability.timeutil import resolve_window

INSTRUCTIONS = """\
Read-only Grafana/Prometheus. Profiles and datasources are fixed by the host; you
cannot pass URLs, headers or credentials. Nothing here writes to Grafana.

- Always give an explicit IANA `tz` (e.g. "UTC", "America/Chicago") and an explicit
  window ("6h", "2d"; max 7d) or start/end. Naive local times in a DST gap or fold are
  rejected: add an offset. The result states absolute UTC and local endpoints.
- Leave `step` empty unless you need one: the default keeps at most 1000 points per
  series. An explicit step that would exceed 2000 points is rejected with the required
  minimum; it is never coarsened for you. Minimum step is 15s.
- Limits: 100 series, 100k samples, 4 MiB response. Overflow is an error: aggregate
  (sum by, topk) or add label matchers; results are never silently truncated.
- ABSENT is not zero. An empty result means no series matched (missing metric, label
  or target). Zero is a real sample. Gaps are missing data, not zero.
- Target health (`ergo_prom_targets`) is scrape health only. up=1 means Prometheus
  fetched the endpoint, not that the application works. No target = unmonitored.
- Native histograms and non-time-series results cannot be plotted. For panels you
  cannot reproduce (non-Prometheus datasource, transformations, variables, library
  panels) give the panel link from `ergo_grafana_link`; do not imitate the panel.
- `ergo_prom_chart` draws a data chart, labeled as not a Grafana screenshot, and attaches
  it to the chat. State its query, range, tz and step when you describe it.
- To change a dashboard: `ergo_grafana_dashboard` reports provisioned/owner. Find the
  owning file in git and open a PR; do not look for a Grafana write path.
"""


def _opt(value):
    return value if value not in ("", None) else None


class ObservabilityPlugin(BotPlugin):
    name = "observability"
    description = (
        "Read-only Grafana dashboards, bounded PromQL, target health and data charts."
    )

    # Injectable for tests.
    clock = staticmethod(lambda: datetime.now(UTC))
    limits = None

    def __init__(self, bot, config=None):
        super().__init__(bot, config)
        unknown = self.config.keys() - {"profiles", "root_only"}
        if unknown:
            msg = f"observability accepts only profiles and root_only, not {sorted(unknown)}"
            raise ValueError(msg)
        raw = self.config.get("profiles") or {}
        if not isinstance(raw, dict) or not raw:
            msg = "observability needs profiles: a mapping of name to profile"
            raise ValueError(msg)
        self.profiles = {
            name: Profile.from_config(name, cfg) for name, cfg in raw.items()
        }
        self.root_only = bool(self.config.get("root_only", False))

    @property
    def skill_instructions(self):
        names = ", ".join(self.profiles)
        return INSTRUCTIONS + f"\nProfiles: {names}.\n"

    # -- helpers ----------------------------------------------------------------

    def profile(self, name: str | None) -> Profile:
        name = _opt(name)
        if name is None:
            if len(self.profiles) == 1:
                return next(iter(self.profiles.values()))
            raise ObservabilityError(
                NOT_CONFIGURED,
                f"profile is required; one of: {', '.join(self.profiles)}",
            )
        if name not in self.profiles:
            raise ObservabilityError(
                NOT_CONFIGURED,
                f"unknown profile {name!r}; one of: {', '.join(self.profiles)}",
            )
        return self.profiles[name]

    def prom(self, ctx, profile, datasource) -> PromClient:
        token = profile.read_token(ctx.secret) if profile.mode == "proxy" else None
        if profile.mode == "proxy" and not token:
            raise ObservabilityError(
                NOT_CONFIGURED, "no Grafana token is available for this profile"
            )
        return PromClient(
            profile, _opt(datasource), token=token, limits=self.limits, clock=self.clock
        )

    def grafana(self, ctx, profile) -> GrafanaClient:
        return GrafanaClient(
            profile, token=profile.read_token(ctx.secret), limits=self.limits
        )

    @staticmethod
    def _guard(fn):
        @functools.wraps(fn)
        def run(*args, **kwargs):
            try:
                return fn(*args, **kwargs)
            except ObservabilityError as exc:
                hint = (
                    f" (minimum step {exc.minimum_step}s)" if exc.minimum_step else ""
                )
                return f"ERROR [{exc.code}] {exc.message}{hint}"

        return run

    # -- tools ------------------------------------------------------------------

    def toolkits(self, ctx):
        if not self._applies(ctx):
            return []
        guard = self._guard
        plugin = self

        @bot_tool(name="ergo_grafana_search", takes_context=True)
        @guard
        def search(
            ctx,
            kind: str = "dashboards",
            query: str = "",
            profile: str = "",
            limit: int = 50,
            page: int = 1,
        ):
            """Search Grafana dashboards (kind=dashboards) or list the allowed datasources (kind=datasources: name, uid, type, default only)."""
            prof = plugin.profile(profile)
            client = plugin.grafana(ctx, prof)
            if kind == "datasources":
                rows = client.datasources()
                body = "\n".join(
                    f"- {r['name']} uid={r['uid']} type={r['type']}{' (default)' if r['default'] else ''}"
                    for r in rows
                )
                return f"profile={prof.name} allowed datasources:\n{body or '(none visible)'}"
            if kind != "dashboards":
                raise ObservabilityError(
                    INVALID_QUERY, "kind must be dashboards or datasources"
                )
            found = client.search_dashboards(query, limit=limit, page=page)
            body = "\n".join(
                f"- {i['uid']}: {i['title']} [{', '.join(i['tags'])}] folder={i['folder'] or '-'} {i['link']}"
                for i in found["items"]
            )
            more = (
                f"\nmore results: page={found['page'] + 1}" if found["has_more"] else ""
            )
            return text.trim(
                f"profile={prof.name} dashboards matching {query!r}:\n{body or '(none)'}{more}"
            )

        @bot_tool(name="ergo_grafana_dashboard", takes_context=True)
        @guard
        def dashboard(ctx, uid: str, profile: str = ""):
            """Summarize one dashboard: panels, queries, datasource uids, variables, provisioned state and owning repo/path (when known)."""
            prof = plugin.profile(profile)
            return text.format_dashboard(plugin.grafana(ctx, prof).dashboard(uid))

        @bot_tool(name="ergo_prom_query", takes_context=True)
        @guard
        def prom_query(
            ctx,
            expr: str,
            tz: str,
            mode: str = "instant",
            window: str = "",
            start: str = "",
            end: str = "",
            time: str = "",
            step: str = "",
            datasource: str = "",
            profile: str = "",
        ):
            """Run a bounded PromQL query. mode=instant (optional `time`) or range (window like 6h, or start+end; optional step). tz is a required IANA timezone."""
            prof = plugin.profile(profile)
            prom = plugin.prom(ctx, prof, datasource)
            if mode == "instant":
                result, at, zone = prom.instant(expr, tz=tz, at=_opt(time))
                link = explore_link(prof, prom.datasource, expr, at, at)
                return text.format_query(
                    result, query=expr, source=prom.source, tz=zone, at=at, link=link
                )
            if mode != "range":
                raise ObservabilityError(INVALID_QUERY, "mode must be instant or range")
            result, win = prom.range(
                expr,
                tz=tz,
                window=_opt(window),
                start=_opt(start),
                end=_opt(end),
                step=_opt(step),
            )
            link = explore_link(prof, prom.datasource, expr, win.start, win.end)
            return text.format_query(
                result, query=expr, source=prom.source, tz=win.tz, window=win, link=link
            )

        @bot_tool(name="ergo_prom_targets", takes_context=True)
        @guard
        def prom_targets(
            ctx,
            job: str = "",
            namespace: str = "",
            include_up: bool = False,
            tz: str = "UTC",
            datasource: str = "",
            profile: str = "",
        ):
            """Current scrape health of Prometheus targets, optionally filtered by job/namespace, kept separate from `up` values. Scrape success is not application health."""
            prof = plugin.profile(profile)
            prom = plugin.prom(ctx, prof, datasource)
            info = prom.targets(job=_opt(job), namespace=_opt(namespace))
            up = ""
            if include_up:
                selector = prom.up_selector(_opt(job), _opt(namespace))
                result, at, zone = prom.instant(selector, tz=tz)
                up = text.format_query(
                    result, query=selector, source=prom.source, tz=zone, at=at
                )
            return text.format_targets(
                info,
                source=prom.source,
                job=_opt(job),
                namespace=_opt(namespace),
                up=up,
            )

        @bot_tool(name="ergo_grafana_link", takes_context=True)
        @guard
        def link(
            ctx,
            uid: str,
            panel_id: int = -1,
            tz: str = "",
            window: str = "",
            start: str = "",
            end: str = "",
            variables: dict | None = None,
            profile: str = "",
        ):
            """Deep link to a dashboard (panel_id=-1) or one panel, with explicit range, timezone and var-* variables (a mapping of name to value)."""
            prof = plugin.profile(profile)
            win = zone = None
            if _opt(window) or _opt(start):
                win = resolve_window(
                    tz=_opt(tz),
                    now=plugin.clock(),
                    window=_opt(window),
                    start=_opt(start),
                    end=_opt(end),
                )
                zone = win.tz
            elif _opt(tz):
                zone = load_tz(tz)
            url = dashboard_link(
                prof,
                uid,
                panel_id=None if panel_id < 0 else panel_id,
                start=win.start if win else None,
                end=win.end if win else None,
                tz=zone,
                variables=variables,
            )
            span = f"\n{win.describe()}" if win else ""
            result = f"profile={prof.name} link: {url}{span}"
            if len(result.encode()) > text.MAX_TEXT_BYTES:
                raise ObservabilityError(
                    BOUNDS_EXCEEDED, "dashboard link output too long"
                )
            return result

        @bot_tool(name="ergo_prom_chart", takes_context=True)
        @guard
        def chart(
            ctx,
            expr: str,
            tz: str,
            window: str = "",
            start: str = "",
            end: str = "",
            step: str = "",
            width: int = 1200,
            height: int = 675,
            datasource: str = "",
            profile: str = "",
        ):
            """Chart a PromQL range query as a PNG attached to the chat. A data chart (query, range, tz, step, source in the image), not a Grafana panel screenshot."""
            prof = plugin.profile(profile)
            charts.check_size(width, height)
            prom = plugin.prom(ctx, prof, datasource)
            result, win = prom.range(
                expr,
                tz=tz,
                window=_opt(window),
                start=_opt(start),
                end=_opt(end),
                step=_opt(step),
            )
            link = explore_link(prof, prom.datasource, expr, win.start, win.end)
            summary = text.format_query(
                result, query=expr, source=prom.source, tz=win.tz, window=win, link=link
            )
            png = charts.render_chart(
                result,
                charts.ChartMeta(expr, prom.source, win, link),
                width=width,
                height=height,
            )
            meta = {
                "kind": "prometheus_data_chart",
                "query": expr,
                "range_utc": [win.start.isoformat(), win.end.isoformat()],
                "tz": win.tz.key,
                "step": win.step,
                "profile": prof.name,
                "datasource": prom.datasource,
                "link": link,
            }
            return plugin.deliver_image(
                ctx,
                "prometheus-chart.png",
                png,
                meta,
                f"{charts.TITLE_LABEL}.\n{summary}",
            )

        tools = [search, dashboard, prom_query, prom_targets, link, chart]

        if any(p.renderer for p in self.profiles.values()):

            @bot_tool(name="ergo_grafana_panel_render", takes_context=True)
            @guard
            def panel_render(
                ctx,
                uid: str,
                panel_id: int,
                tz: str,
                window: str = "",
                start: str = "",
                end: str = "",
                width: int = 1000,
                height: int = 500,
                profile: str = "",
            ):
                """Server-side Grafana panel image, attached only if the renderer produced a real PNG of the requested size."""
                prof = plugin.profile(profile)
                if not prof.renderer:
                    raise ObservabilityError(
                        NOT_CONFIGURED,
                        "server rendering is not enabled for this profile",
                    )
                charts.check_size(width, height)
                win = resolve_window(
                    tz=tz,
                    now=plugin.clock(),
                    window=_opt(window),
                    start=_opt(start),
                    end=_opt(end),
                )
                client = plugin.grafana(ctx, prof)
                response = client.render_panel(
                    uid,
                    panel_id,
                    start=win.start,
                    end=win.end,
                    tz=win.tz,
                    width=width,
                    height=height,
                )
                png = charts.verify_render(response, width, height)
                url = dashboard_link(
                    prof,
                    uid,
                    panel_id=panel_id,
                    start=win.start,
                    end=win.end,
                    tz=win.tz,
                )
                meta = {
                    "kind": "grafana_panel_render",
                    "dashboard": uid,
                    "panel_id": panel_id,
                    "range_utc": [win.start.isoformat(), win.end.isoformat()],
                    "tz": win.tz.key,
                    "profile": prof.name,
                    "link": url,
                }
                return plugin.deliver_image(
                    ctx,
                    "grafana-panel.png",
                    png,
                    meta,
                    f"Grafana server-rendered panel {uid}#{panel_id}.\n{win.describe()}\nlink: {url}",
                )

            tools.append(panel_render)

        return [FunctionToolkit.from_functions(tools, ctx)]

    @staticmethod
    def deliver_image(ctx, filename: str, png: bytes, metadata: dict, body: str):
        """Store the PNG as a real chat attachment and return a reference to it."""
        if ctx.session is None:
            return ToolResult(text.trim(body), [ToolImage(png, name=filename)])
        row = save_session_file(
            ctx.session,
            filename,
            png,
            media_type="image/png",
            source="bot",
            metadata=metadata,
        )
        return ToolResult(
            text.trim(f"{body}\nattachment: {row.id}"), [ToolImage.from_attachment(row)]
        )
