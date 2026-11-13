"""Read-only Grafana: search, dashboard summary, datasources, deep links, ownership."""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING
from urllib.parse import quote
from urllib.parse import urlencode

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
MAX_VARS = 20
MAX_VAR_VALUE = 200
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
    if len(variables) > MAX_VARS:
        raise ObservabilityError(INVALID_QUERY, f"at most {MAX_VARS} variables")
    for name, value in variables.items():
        if not _VAR_NAME.fullmatch(str(name)):
            raise ObservabilityError(INVALID_QUERY, f"invalid variable name {name!r}")
        for item in value if isinstance(value, list) else [value]:
            if len(str(item)) > MAX_VAR_VALUE:
                raise ObservabilityError(
                    INVALID_QUERY, f"variable {name} value too long"
                )
            params.append((f"var-{name}", str(item)))
    url = f"{_public(profile)}/d/{quote(uid, safe='')}"
    return url + ("?" + urlencode(params, quote_via=quote) if params else "")


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
            uid = str(row.get("uid", ""))
            if not UID_RE.fullmatch(uid):
                continue
            items.append(
                {
                    "uid": uid,
                    "title": str(row.get("title", ""))[:200],
                    "tags": [str(t)[:50] for t in (row.get("tags") or [])][:20],
                    "folder": str(row.get("folderTitle", ""))[:200],
                    "link": dashboard_link(self.profile, uid)
                    if self._can_link()
                    else "",
                }
            )
        return {
            "items": items,
            "page": page,
            "limit": limit,
            "has_more": len(rows) >= limit,
        }

    def _can_link(self) -> bool:
        return bool(self.profile.public_url or self.profile.grafana_url)

    def datasources(self) -> list[dict]:
        """Name/uid/type/default of the datasources this profile allows. Nothing else."""
        rows = self.http.get_json("/api/datasources")
        if not isinstance(rows, list):
            raise ObservabilityError(
                UPSTREAM_UNAVAILABLE, "unexpected datasource response"
            )
        allowed = set(self.profile.datasources)
        return [
            {
                "name": str(r.get("name", ""))[:100],
                "uid": str(r.get("uid", "")),
                "type": str(r.get("type", ""))[:100],
                "default": bool(r.get("isDefault", False)),
            }
            for r in rows
            if r.get("uid") in allowed
        ]

    def dashboard(self, uid: str) -> dict:
        """Summary of one dashboard: panels, targets, datasource uids, variables, ownership."""
        payload = self.http.get_json(f"/api/dashboards/uid/{_need_uid(uid)}")
        dash = payload.get("dashboard") if isinstance(payload, dict) else None
        if not isinstance(dash, dict):
            raise ObservabilityError(
                UPSTREAM_UNAVAILABLE, "unexpected dashboard response"
            )
        meta = payload.get("meta") or {}
        panels = []
        for panel in _walk_panels(dash.get("panels") or []):
            if len(panels) >= MAX_PANELS:
                break
            panels.append(_panel_summary(panel))
        variables = [
            {
                "name": str(v.get("name", ""))[:100],
                "type": str(v.get("type", ""))[:50],
                "query": _clip_value(v.get("query")),
                "datasource": _ds_uid(v.get("datasource")),
            }
            for v in (dash.get("templating") or {}).get("list", [])
        ]
        uids = {d for p in panels for d in p["datasources"]}
        uids |= {
            t["datasource"] for p in panels for t in p["targets"] if t["datasource"]
        }
        provisioned = meta.get("provisioned")
        summary = {
            "uid": uid,
            "title": str(dash.get("title", ""))[:200],
            "tags": [str(t)[:50] for t in (dash.get("tags") or [])][:20],
            "folder": str(meta.get("folderTitle", ""))[:200],
            "version": dash.get("version"),
            "slug": str(meta.get("slug", ""))[:128],
            "provisioned": provisioned if isinstance(provisioned, bool) else None,
            "provisioned_file": str(meta.get("provisionedExternalId", ""))[:200],
            "panels": panels,
            "variables": variables,
            "datasource_uids": sorted(uids),
            "link": dashboard_link(self.profile, uid) if self._can_link() else "",
        }
        summary["ownership"] = locate(self.profile, summary)
        return summary

    def render_panel(
        self,
        uid: str,
        slug: str,
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
        if not _SLUG.fullmatch(slug or ""):
            raise ObservabilityError(INVALID_QUERY, "invalid dashboard slug")
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
        return self.http.get(f"/render/d-solo/{_need_uid(uid)}/{slug}", params)


def locate(profile: Profile, summary: dict) -> dict:
    """Who owns this dashboard? Reports facts and config-supplied mapping; never guesses."""
    owner = profile.owner_for(summary["uid"])
    provisioned = summary["provisioned"]
    if provisioned is True:
        edit = "provisioned: edit the owning source in git and open a PR; Grafana edits are not persisted"
    elif provisioned is False:
        edit = "not provisioned: stored in Grafana's database; there is no git owner to PR against"
    else:
        edit = "provisioning state unknown"
    return {
        "provisioned": provisioned,
        "provisioned_file": summary["provisioned_file"] or None,
        "owner": owner or "unknown",
        "how_to_change": edit,
    }


def _walk_panels(panels: list):
    for panel in panels:
        if not isinstance(panel, dict):
            continue
        yield panel
        yield from _walk_panels(panel.get("panels") or [])


def _ds_uid(ds) -> str:
    if isinstance(ds, dict):
        return str(ds.get("uid", ""))[:128]
    if isinstance(ds, str):
        return ds[:128]
    return ""


def _clip_value(value) -> str:
    if isinstance(value, dict):
        value = value.get("query") or value.get("expr") or ""
    return str(value or "")[:EXPR_CLIP]


def _panel_summary(panel: dict) -> dict:
    targets = [
        {
            "ref": str(t.get("refId", ""))[:20],
            "expr": str(t.get("expr") or t.get("rawSql") or t.get("query") or "")[
                :EXPR_CLIP
            ],
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
        "targets": targets,
    }
