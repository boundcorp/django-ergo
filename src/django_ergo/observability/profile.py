"""Host-owned connection profiles. Models never supply any of these values."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from dataclasses import field
from pathlib import Path
from urllib.parse import urlsplit

from .errors import NOT_CONFIGURED
from .errors import ObservabilityError

UID_RE = re.compile(r"[A-Za-z0-9_-]{1,128}")
_KEYS = {
    "grafana_url",
    "public_url",
    "token_env",
    "token_file",
    "datasources",
    "mode",
    "prometheus_url",
    "renderer",
    "ownership",
}
_OWNERSHIP_KEYS = {"match_uid_prefix", "match_uid", "repo", "path_glob", "source"}


def _http_url(value, what: str) -> str:
    parts = urlsplit(value) if isinstance(value, str) else None
    if not parts or parts.scheme not in {"http", "https"} or not parts.hostname:
        msg = f"observability {what} must be an http(s) URL"
        raise ValueError(msg)
    if parts.username or parts.password or parts.query or parts.fragment:
        msg = f"observability {what} must not carry credentials, a query or a fragment"
        raise ValueError(msg)
    return value.rstrip("/")


@dataclass(frozen=True)
class Profile:
    """One Grafana (and optionally direct Prometheus) deployment.

    ``mode`` is ``proxy`` (queries go through Grafana's datasource proxy, the default)
    or ``direct`` (queries go to ``prometheus_url`` with no credential). There is no
    fallback between them.
    """

    name: str
    grafana_url: str = ""
    public_url: str = ""
    token_env: str = ""
    token_file: str = ""
    datasources: tuple[str, ...] = ()
    mode: str = "proxy"
    prometheus_url: str = ""
    renderer: bool = False
    ownership: tuple[dict, ...] = field(default_factory=tuple)

    @classmethod
    def from_config(cls, name: str, raw: dict) -> Profile:
        if not isinstance(raw, dict):
            msg = f"observability profile {name!r} must be a mapping"
            raise ValueError(msg)  # noqa: TRY004
        unknown = raw.keys() - _KEYS
        if unknown:
            msg = f"observability profile {name!r}: unknown keys {sorted(unknown)}"
            raise ValueError(msg)
        mode = raw.get("mode", "proxy")
        if mode not in {"proxy", "direct"}:
            msg = f"observability profile {name!r}: mode must be proxy or direct"
            raise ValueError(msg)
        grafana_url = (
            _http_url(raw["grafana_url"], "grafana_url")
            if raw.get("grafana_url")
            else ""
        )
        prom = (
            _http_url(raw["prometheus_url"], "prometheus_url")
            if raw.get("prometheus_url")
            else ""
        )
        if mode == "proxy" and not grafana_url:
            msg = (
                f"observability profile {name!r}: grafana_url is required in proxy mode"
            )
            raise ValueError(msg)
        if mode == "direct" and not prom:
            msg = f"observability profile {name!r}: prometheus_url is required in direct mode"
            raise ValueError(msg)
        uids = raw.get("datasources") or []
        if not isinstance(uids, list) or not all(
            isinstance(u, str) and UID_RE.fullmatch(u) for u in uids
        ):
            msg = f"observability profile {name!r}: datasources must be a list of datasource uids"
            raise ValueError(msg)
        if mode == "direct" and len(uids) > 1:
            raise ValueError(
                f"observability profile {name!r}: direct mode accepts at most one datasource identity"
            )
        ownership = raw.get("ownership") or []
        if not isinstance(ownership, list):
            msg = f"observability profile {name!r}: ownership must be a list"
            raise ValueError(msg)  # noqa: TRY004
        for rule in ownership:
            if (
                not isinstance(rule, dict)
                or rule.keys() - _OWNERSHIP_KEYS
                or not (rule.get("match_uid_prefix") or rule.get("match_uid"))
            ):
                msg = (
                    f"observability profile {name!r}: each ownership rule needs "
                    f"match_uid_prefix or match_uid, plus repo/path_glob/source"
                )
                raise ValueError(msg)
        return cls(
            name=name,
            grafana_url=grafana_url,
            public_url=_http_url(raw["public_url"], "public_url")
            if raw.get("public_url")
            else "",
            token_env=str(raw.get("token_env") or ""),
            token_file=str(raw.get("token_file") or ""),
            datasources=tuple(uids),
            mode=mode,
            prometheus_url=prom,
            renderer=bool(raw.get("renderer", False)),
            ownership=tuple(dict(r) for r in ownership),
        )

    def check_datasource(self, uid: str | None) -> str:
        """The allowlisted datasource uid to use; anything else fails closed."""
        if not self.datasources:
            raise ObservabilityError(
                NOT_CONFIGURED, "no datasources are allowed for this profile"
            )
        if uid is None or uid == "":
            if len(self.datasources) == 1:
                return self.datasources[0]
            raise ObservabilityError(
                NOT_CONFIGURED,
                f"datasource is required; allowed: {', '.join(self.datasources)}",
            )
        if uid not in self.datasources:
            raise ObservabilityError(
                NOT_CONFIGURED,
                f"datasource {uid!r} is not allowed; allowed: {', '.join(self.datasources)}",
            )
        return uid

    def read_token(self, secret=None) -> str | None:
        """The token, read now: ``secret(token_env)`` (e.g. ``ToolContext.secret``), else the file."""
        if self.token_env:
            value = (secret or os.environ.get)(self.token_env)
            if value:
                return value.strip()
        if self.token_file:
            try:
                value = Path(self.token_file).read_text().strip()
            except OSError:
                value = ""
            if value:
                return value
        return None

    def owner_for(self, uid: str) -> dict | None:
        for rule in self.ownership:
            exact = rule.get("match_uid")
            prefix = rule.get("match_uid_prefix")
            if (exact and uid == exact) or (prefix and uid.startswith(prefix)):
                return {
                    k: rule[k] for k in ("repo", "path_glob", "source") if k in rule
                }
        return None
