"""Read coding-agent workers' token usage again and save it, for workers whose
usage was missed (local_shell Codex jobs, and jobs still running, before the
usage scan covered them).

    ergonaut manage agent_usage                    # look up and save
    ergonaut manage agent_usage --dry-run          # show what would be saved
    ergonaut manage agent_usage --session <uuid>   # one chat's workers only

The lookup runs where the worker's agent manager runs (local_shell: the job
folders and the agents' own session files on this host). A worker whose
manager can't read usage back, or whose files are gone, is left as it was.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from ergonaut.apps.bots.loading import ErgonautConfigError, find_setup, load_registry


class Command(BaseCommand):
    help = "Record coding-agent workers' token usage that was missed."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="Look up without saving.")
        parser.add_argument("--session", default="", help="Only this chat's workers (its id).")
        parser.add_argument("--bot", default="", help="Only this bot's workers.")

    def handle(self, *args, **options):
        from django_ergo.bots.agents import worker_of
        from django_ergo.conversation.models import Worker

        try:
            registry = load_registry(find_setup())
        except (ErgonautConfigError, ValueError, ImportError) as e:
            raise CommandError(str(e)) from e
        workers = Worker.objects.filter(function__startswith="agent:").select_related("session")
        if options["session"]:
            workers = workers.filter(session_id=options["session"])
        if options["bot"]:
            workers = workers.filter(bot_name=options["bot"])
        counts = {"saved": 0, "empty": 0, "skipped": 0}
        total = 0
        for worker in workers.order_by("created_at"):
            found = worker_of(registry.get(worker.bot_name), worker) if worker.bot_name in registry else None
            try:
                models = found[0].record_usage(worker, found[1], dry_run=options["dry_run"]) if found else None
            except Exception as e:  # noqa: BLE001 — one unreadable worker shouldn't stop the rest
                self.stdout.write(f"  ! {worker.bot_name}: {worker.title} ({e})")
                counts["skipped"] += 1
                continue
            if models is None:
                counts["skipped"] += 1
                continue
            if not models:
                counts["empty"] += 1
                continue
            tokens = sum(
                sum(int(row.get(part) or 0) for part in ("input", "cache_write", "cache_read", "output"))
                for row in models.values()
            )
            total += tokens
            counts["saved"] += 1
            self.stdout.write(f"  + {worker.bot_name}: {worker.title} -> {tokens:,} tokens ({', '.join(models)})")
        verb = "Would save" if options["dry_run"] else "Saved"
        self.stdout.write(
            f"{verb} usage for {counts['saved']} workers ({total:,} tokens); {counts['empty']} had no usage "
            f"to read and {counts['skipped']} have a manager that can't read it back."
        )
