"""Native RunPod worker and ownership-scoped tools (no AgentManager bridge)."""

from __future__ import annotations

import time

from django_ergo.bots.plugins import BotPlugin
from django_ergo.bots.tools import FunctionToolkit
from django_ergo.bots.tools import bot_tool
from django_ergo.bots.workers import cancel

from . import native
from . import native_h3
from .experiment import Experiment
from .models import RunpodJob


class RunpodPlugin(BotPlugin):
    name = "runpod"
    description = (
        "Approved GPU rental profiles, native workers, collection and durable cleanup."
    )

    def __init__(self, bot, config=None):
        super().__init__(bot, config)
        unknown = self.config.keys() - {"account", "profiles", "root_only"}
        if unknown:
            message = "RunPod bot config accepts only account, profiles and root_only"
            raise ValueError(message)
        self.account = self.config.get("account", "")
        self.profiles = self.config.get("profiles", [])
        self.root_only = self.config.get("root_only", True)

    @property
    def skill_instructions(self):
        return (
            "List reviewed profiles before starting. Start requires explicit human "
            "spend approval and a stable request_key. The Worker picks the GPU and "
            "region from live stock inside the profile's declared envelope, falls "
            "back to the next offer when one is refused for capacity, and waits for "
            "stock up to the profile's deadline; no further approval is needed for "
            "that choice. Use ergo_runpod_stock to see what would launch now and "
            "status for the chosen offer and fallback attempts. A Worker reports "
            "progress and completion; cancelling it requests stop through "
            "independent maintenance. Only cleanup_complete proves rental cleanup. "
            "Never retry an unknown create or generation, invent a replacement or "
            "use a standalone Job. Collect preserves ordered partial outputs; "
            "reservation is not an invoice."
        )

    def worker_functions(self):
        return {"watch": self.watch}

    def toolkits(self, ctx):
        if not self._applies(ctx):
            return []

        @bot_tool(name="ergo_runpod_profiles")
        def profiles():
            """List the bot's approved workload profiles without provider access."""
            return native.profiles(self.account, self.profiles, ctx)

        @bot_tool(name="ergo_runpod_stock", takes_context=True)
        def stock(ctx, profile: str):
            """Read live RunPod stock for a profile: eligible offers best-first and why others are skipped."""
            return native.stock(self.account, self.profiles, profile, ctx)

        @bot_tool(name="ergo_runpod_start", requires_approval=True, takes_context=True)
        def start(ctx, profile: str, request_key: str):
            """Start one approved frozen workload as a native Worker, never a replacement rental."""
            return native.start(ctx, self.account, self.profiles, profile, request_key)

        @bot_tool(name="ergo_runpod_status", takes_context=True)
        def status(ctx, job_id: str):
            """Read this chat's durable job, rental/cleanup state, chosen offer, stock polls and fallback attempts."""
            return native.status(native.owned(ctx, self.account, job_id))

        @bot_tool(name="ergo_runpod_collect", takes_context=True)
        def collect(ctx, job_id: str):
            """Idempotently collect already exported ordered artifacts, including partial outputs."""
            return native.collect(native.owned(ctx, self.account, job_id))

        @bot_tool(name="ergo_runpod_terminate", takes_context=True)
        def terminate(ctx, job_id: str):
            """Persist monotonic stop before bounded cleanup; never wait for new spend approval."""
            job = native.owned(ctx, self.account, job_id)
            native.stop(job)
            if job.worker_id:
                cancel(job.worker)
            native.attempt_cleanup(job)
            return native.status(job)

        return [
            FunctionToolkit.from_functions(
                [profiles, stock, start, status, collect, terminate], ctx
            )
        ]

    def watch(self, ctx, job_id):
        job = RunpodJob.objects.select_related("session", "worker").get(pk=job_id)
        if (
            job.bot_name != self.bot.name
            or job.account_id != self.account
            or job.worker_id != ctx.worker.pk
            or job.session_id != ctx.session.pk
        ):
            message = "RunPod Worker ownership mismatch"
            raise PermissionError(message)
        experiment = Experiment(job.ledger_path)
        try:
            if native.invalid_owner(job) or ctx.stopping:
                native.stop(job, "worker_lost")
            with native.operation(job) as acquired:
                if not acquired:
                    return ctx.again(
                        5, progress="RunPod controller busy; durable state unchanged"
                    )
                state = experiment.status()
                if not state["stop_reason"]:
                    progress = native_h3.step(ctx, job)
                    ctx.activity(
                        [{"kind": "progress", "text": progress, "at": time.time()}],
                        source="runpod",
                        checked_at=time.time(),
                        waiting=progress,
                        liveness="controller observed",
                        stall_after=180,
                    )
                    ctx.progress(progress)
        except Exception as exc:  # noqa: BLE001 — raw provider errors may contain capabilities
            native.fail(job, exc)
        finally:
            # Not an unconditional teardown on Again: only durable terminal intent.
            if experiment.status()["stop_reason"]:
                native.attempt_cleanup(job)
        state = experiment.status()
        if state["phase"] == "terminated":
            result = native.collect(job)
            if state["stop_reason"] != "success":
                message = f"RunPod failed ({job.error or state['stop_reason']}); cleanup confirmed"
                raise RuntimeError(message)
            return result
        if state["stop_reason"] and (job.error or time.time() >= job.deadline):
            message = f"RunPod stopped ({job.error or state['stop_reason']}); cleanup remains pending"
            raise RuntimeError(message)
        return ctx.again(
            5,
            progress=(
                "RunPod cleanup pending"
                if state["stop_reason"]
                else ctx.worker.progress
            ),
        )
