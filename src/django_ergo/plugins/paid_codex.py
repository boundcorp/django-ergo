"""Opt-in supervised Codex proxy manager; refuses without external confinement.

No production backend is implemented. Settings, a dotted class and self-reported
probe booleans cannot establish confinement. The launch gate stays closed until
a reviewed runtime can create, probe and retain the SAME boundary for execution.
"""

# ruff: noqa: SLF001 -- shared accounting locks and durable finish
from __future__ import annotations

import json
import os
from contextvars import ContextVar
from decimal import Decimal
from urllib.parse import urlsplit

from django_ergo.bots import pools
from django_ergo.conversation.paid_agent_proxy import issue
from django_ergo.conversation.paid_agent_proxy import mapped_account
from django_ergo.conversation.paid_agent_proxy import revoke
from django_ergo.plugins.local_shell import LocalShellAgents
from django_ergo.plugins.local_shell import LocalShellError
from django_ergo.plugins.local_shell import LocalShellPlugin

_issued = ContextVar("paid_codex_launch", default=None)


class PaidCodexPlugin(LocalShellPlugin):
    name = "paid_codex"
    description = "Supervised, proxy-metered Codex with mandatory external confinement"

    def on_load(self):
        super().on_load()
        self.agents = ("codex",)
        self.proxy_url = str(self.config.get("proxy_url") or "")
        parsed = urlsplit(self.proxy_url)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.path != "/v1"
        ):
            msg = "paid_codex requires an HTTPS proxy URL ending in /v1"
            raise LocalShellError(msg)
        self.manager = PaidCodexManager(self)

    def start_agent(self, ctx, spec):
        if not spec.confinement or spec.agent != "codex":
            msg = "paid_codex only accepts pool-authorized Codex launches"
            raise LocalShellError(msg)
        folder = self.folder(spec.workspace)
        try:
            self.require_runtime()
        except LocalShellError:
            self.release_unstarted(ctx, spec)
            raise
        if self.command_prefix or self.secrets or self.template:
            msg = "Paid launches refuse ambient command prefixes, secret injection and templates"
            raise LocalShellError(msg)
        # Check presence only; never read or display credential file contents.
        self.check_visible_credentials(folder)
        marker = _issued.set(None)
        try:
            handle = super().start_agent(ctx, spec)
            handle["paid_launch"] = str(_issued.get())
            return handle
        except BaseException:
            if _issued.get():
                revoke(_issued.get())
            raise
        finally:
            _issued.reset(marker)

    @staticmethod
    def release_unstarted(ctx, spec):
        """No runtime, process or capability exists, so this allocation is unsent."""
        from django.db import transaction

        from django_ergo.conversation import spend_guard
        from django_ergo.conversation.models import InferenceRequest
        from django_ergo.conversation.models import PaidAgentLaunch
        from django_ergo.conversation.weekly_budgets import lock_family

        pk = spec.confinement.get("reservation_id")
        if not pk or ctx is None:
            return
        with transaction.atomic():
            row = InferenceRequest.objects.filter(
                pk=pk,
                user_id=ctx.session.user_id,
                purpose="coding_launch",
                pool=spend_guard.ledger_key(spec.confinement.get("grant_id")),
            ).first()
            if row is None:
                return
            lock_family(row.provider_family_id or row.provider)
            envelope = spend_guard._envelope_of(spec.confinement.get("grant_id"))
            if envelope is None:
                return
            for key in spend_guard._locks_of(envelope):
                spend_guard.lock(key)
            row = InferenceRequest.objects.select_for_update().get(pk=pk)
            if (
                row.outcome == "pending"
                and not PaidAgentLaunch.objects.filter(reservation=row).exists()
            ):
                spend_guard._finish(
                    pk, outcome="not_sent", cost_status="not_sent", cost_usd=Decimal(0)
                )

    @staticmethod
    def require_runtime():
        # Deliberately no settings-driven extension point. An arbitrary prepare()
        # implementation or claimed sandbox cannot authorize a paid process.
        msg = "Paid launch refused: no verified filesystem/process/network confinement backend"
        raise LocalShellError(msg)

    @staticmethod
    def check_visible_credentials(folder):
        if any(
            os.environ.get(k)
            for k in (
                "OPENAI_BASE_URL",
                "OPENAI_API_KEY",
                "CODEX_API_KEY",
                "ANTHROPIC_BASE_URL",
                "ANTHROPIC_API_KEY",
                "FIREWORKS_API_KEY",
            )
        ):
            msg = "Paid launch refused: ambient provider endpoint or alternate key is visible"
            raise LocalShellError(msg)
        names = {
            ".env",
            "auth.json",
            "credentials.json",
            ".credentials.json",
            "config.toml",
        }
        if folder.exists() and any(
            p.name in names or p.name.startswith(".env.") for p in folder.rglob("*")
        ):
            msg = "Paid launch refused: workspace may expose provider credentials or CLI login/config"
            raise LocalShellError(msg)

    def env(self, ctx, confinement=None, folder=None):
        # The trusted supervisor gets only this launch capability, never a provider key.
        if not confinement or not folder:
            msg = "Proxy launch requires confinement"
            raise LocalShellError(msg)
        clean = {
            k: os.environ[k]
            for k in ("PATH", "LANG", "LC_ALL", "TZ")
            if k in os.environ
        }
        home = folder / ".ergo" / "proxy-home"
        home.mkdir(parents=True, exist_ok=False)
        (home / "codex").mkdir()
        (home / "config").mkdir()
        clean.update(
            HOME=str(home),
            CODEX_HOME=str(home / "codex"),
            XDG_CONFIG_HOME=str(home / "config"),
        )
        decision = pools.Decision(
            consumer=pools.CODING,
            pool=confinement["pool"],
            provider=confinement["provider"],
            model=confinement["model"],
            route=f"{confinement['provider']}/{confinement['model']}",
            billing="metered",
            grant_id=confinement["grant_id"],
            max_output_tokens=confinement["max_output_tokens"],
            transport_identity=confinement["transport_identity"],
            privacy=pools.privacy_for(ctx.bot, ctx.session),
            bot_lineage=tuple(confinement["bot_lineage"]),
            origin=tuple(confinement["origin"]),
        )
        guard = decision.guard(ctx.bot, ctx.session, purpose="paid_agent")
        provider = ctx.bot.providers.providers[guard.provider]
        if provider.type != "openai":
            msg = "Paid Codex requires an explicitly mapped OpenAI account"
            raise LocalShellError(msg)
        authority = {
            "bot": ctx.bot.name,
            "session": str(ctx.session.pk),
            "pool": decision.pool,
            "transport_identity": decision.transport_identity,
            "bot_lineage": list(decision.bot_lineage),
            "origin": list(decision.origin),
        }
        launch, token = issue(
            guard=guard,
            reservation_id=confinement["reservation_id"],
            account_ref=mapped_account(decision.transport_identity),
            authority=authority,
            max_attempts=int(self.config.get("max_attempts", 8)),
            ttl=min(int(self.timeout_seconds), 3600),
            cap_usd=Decimal(str(self.config.get("max_launch_usd", "1"))),
        )
        clean.update(ERGO_PROXY_TOKEN=token, ERGO_PROXY_URL=self.proxy_url)
        _issued.set(launch.pk)
        return clean

    def launch(self, ctx, folder, agent, argv, number, **kwargs):
        # Backend must run BEFORE a token is issued or a supervisor is started.
        # It verifies actual deny-all egress except proxy, no host home/proc,
        # sockets/metadata/service tokens/keys, including descendants, and returns
        # a fixed executable prefix that creates that verified sandbox on exec.
        self.check_visible_credentials(folder)
        prefix = self.require_runtime()
        if (
            not prefix
            or not isinstance(prefix, list)
            or not all(isinstance(p, str) for p in prefix)
        ):
            msg = "Confinement backend did not produce a verified sandbox command"
            raise LocalShellError(msg)
        try:
            return super().launch(
                ctx, folder, agent, [*prefix, *argv], number, **kwargs
            )
        except BaseException:
            if _issued.get():
                revoke(_issued.get())
            raise

    def confined_argv(self, agent, confinement):
        if agent != "codex" or confinement.get("engine") != "openai":
            msg = "Only Codex/OpenAI paid routes are supported"
            raise LocalShellError(msg)
        config = {
            "model_provider": "ergo_proxy",
            "model_providers.ergo_proxy.name": "Ergo metered proxy",
            "model_providers.ergo_proxy.base_url": self.proxy_url,
            "model_providers.ergo_proxy.env_key": "ERGO_PROXY_TOKEN",
            "model_providers.ergo_proxy.wire_api": "responses",
            "model_providers.ergo_proxy.requires_openai_auth": False,
            "model_providers.ergo_proxy.supports_websockets": False,
            "model_providers.ergo_proxy.request_max_retries": 0,
            "model_providers.ergo_proxy.stream_max_retries": 0,
        }
        flags = ["--model", confinement["model"]]
        for key, value in config.items():
            flags.extend(["-c", f"{key}={json.dumps(value)}"])
        return flags

    def agent_managers(self):
        return {self.manager.name: self.manager}

    def check(self, ctx, handle):
        result = super().check(ctx, handle)
        if result.status != "running":
            revoke(handle["paid_launch"])
        return result

    def stop(self, handle):
        revoke(handle["paid_launch"])
        return super().stop(handle)


class PaidCodexManager(LocalShellAgents):
    name = "paid_codex"
    metered_confinement = True
    metered_spend_bound = True
