"""Read-only Grafana: search, dashboard summary, datasources, deep links, ownership."""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING
from urllib.parse import quote
from urllib.parse import urlencode

from .errors import BOUNDS_EXCEEDED
from .errors import FORBIDDEN
from .errors import INVALID_QUERY
from .errors import NOT_CONFIGURED
from .errors import UPSTREAM_UNAVAILABLE
from .errors import ObservabilityError
from .http import BoundedClient
from .http import Limits
from .profile import UID_RE
from .profile import Profile

if TYPE_CHECKING:
    from datetime import datetime
    from zoneinfo import ZoneInfo

MAX_PAGE = 100
MAX_ITEMS = 1000
MAX_DATASOURCES = 1000
MAX_VARS = 20
MAX_VAR_VALUE = 200
MAX_VAR_VALUES = 100
MAX_LINK_BYTES = 30000
MAX_INVENTORY_ITEMS = 10000
MAX_PANELS = 200
MAX_TARGETS_PER_PANEL = 20
EXPR_CLIP = 500

_PATHS = [
    re.compile(r"/api/search"),
    re.compile(r"/api/datasources"),
    re.compile(r"/api/dashboards/uid/[A-Za-z0-9_-]{1,128}"),
    re.compile(r"/render/d-solo/[A-Za-z0-9_-]{1,128}/[A-Za-z0-9_-]{1,128}"),
]
_VAR_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")
_SLUG = re.compile(r"[A-Za-z0-9_-]{1,128}")


def _ms(moment: datetime) -> str:
    return str(int(moment.timestamp() * 1000))


def _need_uid(uid: str) -> str:
    if not isinstance(uid, str) or not UID_RE.fullmatch(uid):
        raise ObservabilityError(INVALID_QUERY, "invalid dashboard uid")
    return uid


def _public(profile: Profile) -> str:
    base = profile.public_url or profile.grafana_url
    if not base:
        raise ObservabilityError(NOT_CONFIGURED, "profile has no public_url for links")
    return base


def dashboard_link(
    profile: Profile,
    uid: str,
    *,
    panel_id: int | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
    tz: ZoneInfo | None = None,
    variables: dict[str, str | list[str]] | None = None,
) -> str:
    """A dashboard or panel URL on the host-configured public origin, fully encoded."""
    _need_uid(uid)
    params: list[tuple[str, str]] = []
    if panel_id is not None:
        if isinstance(panel_id, bool) or not isinstance(panel_id, int) or panel_id < 0:
            raise ObservabilityError(
                INVALID_QUERY, "panel_id must be a non-negative integer"
            )
        params.append(("viewPanel", str(panel_id)))
    if start is not None and end is not None:
        params += [("from", _ms(start)), ("to", _ms(end))]
    if tz is not None:
        params.append(("timezone", tz.key))
    variables = variables or {}
    if not isinstance(variables, dict) or len(variables) > MAX_VARS:
        raise ObservabilityError(INVALID_QUERY, f"at most {MAX_VARS} variables")
    url = f"{_public(profile)}/d/{quote(uid, safe='')}"
    parts = [urlencode([p], quote_via=quote) for p in params]
    size = len(url.encode()) + sum(len(p) + 1 for p in parts)
    for name, value in variables.items():
        if not isinstance(name, str) or not _VAR_NAME.fullmatch(name):
            raise ObservabilityError(INVALID_QUERY, "invalid variable name")
        values = value if isinstance(value, list) else [value]
        if len(values) > MAX_VAR_VALUES:
            raise ObservabilityError(BOUNDS_EXCEEDED, "too many variable values")
        for item in values:
            if not isinstance(item, str) or len(item) > MAX_VAR_VALUE:
                raise ObservabilityError(INVALID_QUERY, "invalid variable value")
            part = urlencode([(f"var-{name}", item)], quote_via=quote)
            size += len(part) + 1
            if size > MAX_LINK_BYTES:
                raise ObservabilityError(BOUNDS_EXCEEDED, "dashboard link too long")
            parts.append(part)
    if size > MAX_LINK_BYTES:
        raise ObservabilityError(BOUNDS_EXCEEDED, "dashboard link too long")
    return url + ("?" + "&".join(parts) if parts else "")


def explore_link(
    profile: Profile, datasource: str, expr: str, start: datetime, end: datetime
) -> str:
    """A Grafana Explore link for a PromQL expression (empty if no public origin)."""
    if not (profile.public_url or profile.grafana_url) or not datasource:
        return ""
    panes = {
        "a": {
            "datasource": datasource,
            "queries": [
                {
                    "refId": "A",
                    "expr": expr,
                    "datasource": {"type": "prometheus", "uid": datasource},
                }
            ],
            "range": {"from": _ms(start), "to": _ms(end)},
        }
    }
    query = urlencode(
        {"schemaVersion": "1", "panes": json.dumps(panes, separators=(",", ":"))},
        quote_via=quote,
    )
    return f"{_public(profile)}/explore?{query}"


class GrafanaClient:
    def __init__(
        self, profile: Profile, *, token: str | None, limits: Limits | None = None
    ):
        if not profile.grafana_url:
            raise ObservabilityError(NOT_CONFIGURED, "profile has no grafana_url")
        if not token:
            raise ObservabilityError(
                NOT_CONFIGURED, "no Grafana token is available for this profile"
            )
        self.profile = profile
        self.http = BoundedClient(
            profile.grafana_url, allowed=_PATHS, token=token, limits=limits
        )

    def search_dashboards(
        self, query: str = "", *, limit: int = 50, page: int = 1
    ) -> dict:
        limit = max(1, min(int(limit), MAX_PAGE))
        page = max(1, int(page))
        if page * limit > MAX_ITEMS + limit:
            raise ObservabilityError(
                INVALID_QUERY, f"at most {MAX_ITEMS} dashboards per search"
            )
        rows = self.http.get_json(
            "/api/search",
            {
                "type": "dash-db",
                "query": str(query)[:200],
                "limit": limit,
                "page": page,
            },
        )
        if not isinstance(rows, list):
            raise ObservabilityError(UPSTREAM_UNAVAILABLE, "unexpected search response")
        items = []
        for row in rows[:limit]:
            if not isinstance(row, dict) or not isinstance(row.get("tags", []), list):
                raise ObservabilityError(
                    UPSTREAM_UNAVAILABLE, "unexpected search response"
                )
            safe_row = self._sanitize(row)
            uid = str(safe_row.get("uid", ""))
            if not UID_RE.fullmatch(uid):
                continue
            items.append(
                {
                    "uid": uid,
                    "title": str(safe_row.get("title", ""))[:200],
                    "tags": [str(t)[:50] for t in (safe_row.get("tags") or [])][:20],
                    "folder": str(safe_row.get("folderTitle", ""))[:200],
                    "link": dashboard_link(self.profile, uid)
                    if self._can_link()
                    else "",
                }
            )
        return self._sanitize(
            {
                "items": items,
                "page": page,
                "limit": limit,
                "has_more": len(rows) >= limit,
            }
        )

    def _can_link(self) -> bool:
        return bool(self.profile.public_url or self.profile.grafana_url)

    def _sanitize(self, value):
        if isinstance(value, str):
            return self.http.sanitize(value, limit=max(len(value), 1) * 2 + 100)
        if isinstance(value, list):
            return [self._sanitize(v) for v in value]
        if isinstance(value, dict):
            return {self._sanitize(k): self._sanitize(v) for k, v in value.items()}
        return value

    def datasources(self) -> list[dict]:
        """Name/uid/type/default of the datasources this profile allows. Nothing else."""
        rows = self.http.get_json("/api/datasources")
        if not isinstance(rows, list):
            raise ObservabilityError(
                UPSTREAM_UNAVAILABLE, "unexpected datasource response"
            )
        if len(rows) > MAX_DATASOURCES:
            raise ObservabilityError(BOUNDS_EXCEEDED, "datasource inventory too large")
        if any(not isinstance(r, dict) for r in rows):
            raise ObservabilityError(
                UPSTREAM_UNAVAILABLE, "unexpected datasource response"
            )
        allowed = set(self.profile.datasources)
        result = []
        for row in rows:
            if not isinstance(row.get("uid"), str) or row["uid"] not in allowed:
                continue
            safe_row = self._sanitize(row)
            result.append(
                {
                    "name": str(safe_row.get("name", ""))[:100],
                    "uid": safe_row["uid"],
                    "type": str(safe_row.get("type", ""))[:100],
                    "default": bool(safe_row.get("isDefault", False)),
                }
            )
        return result

    def _dashboard_data(self, uid: str):
        payload = self.http.get_json(f"/api/dashboards/uid/{_need_uid(uid)}")
        if not isinstance(payload, dict) or not isinstance(
            payload.get("dashboard"), dict
        ):
            raise ObservabilityError(
                UPSTREAM_UNAVAILABLE, "unexpected dashboard response"
            )
        dash = payload["dashboard"]
        meta = payload.get("meta", {})
        templating = dash.get("templating", {})
        if not isinstance(meta, dict) or not isinstance(templating, dict):
            raise ObservabilityError(
                UPSTREAM_UNAVAILABLE, "unexpected dashboard response"
            )
        variables = templating.get("list", [])
        if not isinstance(variables, list) or any(
            not isinstance(v, dict) for v in variables
        ):
            raise ObservabilityError(
                UPSTREAM_UNAVAILABLE, "unexpected dashboard variables"
            )
        if not isinstance(dash.get("tags", []), list):
            raise ObservabilityError(UPSTREAM_UNAVAILABLE, "unexpected dashboard tags")
        panels = list(_walk_panels(dash.get("panels", [])))
        return dash, meta, variables, panels

    def dashboard(self, uid: str) -> dict:
        """Bounded display summary with a full, bounded datasource inventory."""
        dash, meta, variables, all_panels = self._dashboard_data(uid)
        dash, meta, variables, all_panels = self._sanitize(
            [dash, meta, variables, all_panels]
        )
        panels = [_panel_summary(p) for p in all_panels[:MAX_PANELS]]
        uids = set()
        for panel in all_panels:
            ds = _ds_uid(panel.get("datasource"))
            if ds:
                uids.add(ds)
            for target in panel.get("targets", []):
                ds = _ds_uid(target.get("datasource"))
                if ds:
                    uids.add(ds)
        provisioned = meta.get("provisioned")
        summary = {
            "uid": uid,
            "title": str(dash.get("title", ""))[:200],
            "tags": [str(t)[:50] for t in dash.get("tags", [])][:20],
            "folder": str(meta.get("folderTitle", ""))[:200],
            "version": dash.get("version"),
            "slug": str(meta.get("slug", ""))[:128],
            "provisioned": provisioned if isinstance(provisioned, bool) else None,
            "provisioned_file": str(meta.get("provisionedExternalId", ""))[:200],
            "panels": panels,
            "panels_truncated": len(all_panels) > MAX_PANELS,
            "variables": [
                {
                    "name": str(v.get("name", ""))[:100],
                    "type": str(v.get("type", ""))[:50],
                    "query": _clip_value(v.get("query")),
                    "datasource": _ds_uid(v.get("datasource")),
                }
                for v in variables
            ],
            "datasource_uids": sorted(uids),
            "link": dashboard_link(self.profile, uid) if self._can_link() else "",
        }
        summary["ownership"] = locate(self.profile, summary)
        return self._sanitize(summary)

    def authorized_render_slug(self, uid: str, panel_id: int) -> str:
        """Authorize against the complete raw dashboard, never its display summary."""
        if isinstance(panel_id, bool) or not isinstance(panel_id, int) or panel_id < 0:
            raise ObservabilityError(INVALID_QUERY, "invalid panel id")
        dash, meta, variables, panels = self._dashboard_data(uid)
        # Grafana may evaluate dashboard variables and annotations independently of
        # the selected panel; their datasource resolution is not available here.
        if variables:
            raise ObservabilityError(
                FORBIDDEN, "dashboard variables cannot be authorized"
            )
        annotations = dash.get("annotations", {})
        if not isinstance(annotations, dict) or annotations.get("list", []):
            raise ObservabilityError(
                FORBIDDEN, "dashboard annotations cannot be authorized"
            )
        matches = [p for p in panels if p.get("id") == panel_id]
        if len(matches) != 1:
            raise ObservabilityError(FORBIDDEN, "panel cannot be authorized")
        panel = matches[0]
        if panel.get("libraryPanel") is not None:
            raise ObservabilityError(FORBIDDEN, "library panel cannot be authorized")

        def check(ds):
            if not isinstance(ds, dict) or ds.get("type") in ("mixed", "-- Mixed --"):
                raise ObservabilityError(
                    FORBIDDEN, "panel datasource cannot be resolved"
                )
            ref = ds.get("uid")
            if (
                not isinstance(ref, str)
                or not UID_RE.fullmatch(ref)
                or ref not in self.profile.datasources
            ):
                raise ObservabilityError(
                    FORBIDDEN, "panel datasource is not authorized"
                )

        pending = [panel]
        while pending:
            value = pending.pop()
            if isinstance(value, dict):
                if "libraryPanel" in value:
                    raise ObservabilityError(
                        FORBIDDEN, "library panel cannot be authorized"
                    )
                for key, child in value.items():
                    if key == "datasource":
                        check(child)
                    elif key in ("datasourceUid", "datasourceId"):
                        raise ObservabilityError(
                            FORBIDDEN, "panel datasource cannot be resolved"
                        )
                    else:
                        pending.append(child)
            elif isinstance(value, list):
                pending.extend(value)
        inherited = panel.get("datasource")
        targets = panel.get("targets", [])
        if not targets and inherited is None:
            raise ObservabilityError(FORBIDDEN, "panel datasource cannot be resolved")
        for target in targets:
            if target.get("datasource", inherited) is None:
                raise ObservabilityError(
                    FORBIDDEN, "panel datasource cannot be resolved"
                )
        slug = meta.get("slug") or uid
        if not isinstance(slug, str) or not _SLUG.fullmatch(slug):
            raise ObservabilityError(UPSTREAM_UNAVAILABLE, "invalid dashboard slug")
        return slug

    def render_panel(
        self,
        uid: str,
        panel_id: int,
        *,
        start: datetime,
        end: datetime,
        tz: ZoneInfo,
        width: int,
        height: int,
        variables: dict | None = None,
    ):
        """Raw renderer response (caller must verify it is a real PNG)."""
        slug = self.authorized_render_slug(uid, panel_id)
        params = {
            "panelId": int(panel_id),
            "width": int(width),
            "height": int(height),
            "from": _ms(start),
            "to": _ms(end),
            "tz": tz.key,
        }
        for name, value in (variables or {}).items():
            if not _VAR_NAME.fullmatch(str(name)):
                raise ObservabilityError(
                    INVALID_QUERY, f"invalid variable name {name!r}"
                )
            params[f"var-{name}"] = str(value)[:MAX_VAR_VALUE]
        response = self.http.get(f"/render/d-solo/{_need_uid(uid)}/{slug}", params)
        if self.http.token and self.http.token.encode() in response.body:
            raise ObservabilityError(
                UPSTREAM_UNAVAILABLE, "renderer returned unsafe content"
            )
        return response


def locate(profile: Profile, summary: dict) -> dict:
    """Who owns this dashboard? Reports facts and config-supplied mapping; never guesses."""
    owner = profile.owner_for(summary["uid"])
    provisioned = summary["provisioned"]
    if provisioned is True:
        status = "provisioned; Grafana edits are not persisted"
    elif provisioned is False:
        status = "not provisioned; stored in Grafana's database"
    else:
        status = "provisioning state unknown"
    edit = status + (
        "; follow the configured ownership mapping" if owner else "; ownership unknown"
    )
    return {
        "provisioned": provisioned,
        "provisioned_file": summary["provisioned_file"] or None,
        "owner": owner or "unknown",
        "how_to_change": edit,
    }


def _walk_panels(panels: list):
    if not isinstance(panels, list):
        raise ObservabilityError(UPSTREAM_UNAVAILABLE, "unexpected dashboard panels")
    pending = list(reversed(panels))
    count = 0
    while pending:
        panel = pending.pop()
        count += 1
        if count > MAX_INVENTORY_ITEMS:
            raise ObservabilityError(BOUNDS_EXCEEDED, "dashboard inventory too large")
        if not isinstance(panel, dict):
            raise ObservabilityError(UPSTREAM_UNAVAILABLE, "unexpected dashboard panel")
        targets = panel.get("targets", [])
        children = panel.get("panels", [])
        if not isinstance(targets, list) or any(
            not isinstance(t, dict) for t in targets
        ):
            raise ObservabilityError(
                UPSTREAM_UNAVAILABLE, "unexpected dashboard targets"
            )
        if not isinstance(children, list):
            raise ObservabilityError(
                UPSTREAM_UNAVAILABLE, "unexpected dashboard panels"
            )
        count += len(targets)
        if count > MAX_INVENTORY_ITEMS:
            raise ObservabilityError(BOUNDS_EXCEEDED, "dashboard inventory too large")
        yield panel
        pending.extend(reversed(children))


def _ds_uid(ds) -> str:
    if isinstance(ds, dict):
        return str(ds.get("uid", ""))[:128]
    if isinstance(ds, str):
        return ds[:128]
    return ""


def _clip_value(value) -> str:
    if isinstance(value, dict):
        value = value.get("query") or value.get("expr") or ""
    value = str(value or "")
    return value[:EXPR_CLIP] + (" [clipped]" if len(value) > EXPR_CLIP else "")


def _panel_summary(panel: dict) -> dict:
    targets = [
        {
            "ref": str(t.get("refId", ""))[:20],
            "expr": _clip_value(t.get("expr") or t.get("rawSql") or t.get("query")),
            "datasource": _ds_uid(t.get("datasource"))
            or _ds_uid(panel.get("datasource")),
        }
        for t in (panel.get("targets") or [])[:MAX_TARGETS_PER_PANEL]
        if isinstance(t, dict)
    ]
    return {
        "id": panel.get("id"),
        "title": str(panel.get("title", ""))[:200],
        "type": str(panel.get("type", ""))[:50],
        "datasources": [d for d in [_ds_uid(panel.get("datasource"))] if d],
        "targets_truncated": len(panel.get("targets", [])) > MAX_TARGETS_PER_PANEL,
        "targets": targets,
    }
