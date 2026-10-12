"""Record which repository each coding-agent worker ran in, for workers from before
the usage scan recorded it, so Costs can group agent work by project.

    ergonaut manage agent_projects            # look up and save
    ergonaut manage agent_projects --dry-run  # show what would be saved

The lookup runs git in the worker's worktree, where its agent manager runs (Orca:
locally or over ``files_host`` ssh). A worktree that's gone is recorded as an
unresolved project named after its folder; ``--skip-unresolved`` leaves those out.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError
from django.db.models import Q

from ergonaut.apps.bots.loading import ErgonautConfigError, find_setup, load_registry


class Command(BaseCommand):
    help = "Record the repository of coding-agent workers that don't have one yet."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="Look up without saving.")
        parser.add_argument("--skip-unresolved", action="store_true", help="Don't save worktrees that are gone.")
        parser.add_argument("--all", action="store_true", help="Look up workers that already have a project too.")

    def handle(self, *args, **options):
        from django_ergo.bots.agents import worker_of
        from django_ergo.conversation.models import Worker

        try:
            registry = load_registry(find_setup())
        except (ErgonautConfigError, ValueError, ImportError) as e:
            raise CommandError(str(e)) from e
        workers = Worker.objects.filter(Q(function__startswith="agent:") | Q(function="orca:watch"))
        if not options["all"]:
            workers = workers.exclude(state__has_key="project")
        counts = {"saved": 0, "unresolved": 0, "missing": 0}
        for worker in workers.order_by("created_at"):
            if worker.bot_name not in registry:
                counts["missing"] += 1
                continue
            found = worker_of(registry.get(worker.bot_name), worker)
            project = found[0].project(worker, found[1]) if found else None
            if not project:
                counts["missing"] += 1
                self.stdout.write(f"  ? {worker.bot_name}: {worker.title} (no worktree to look in)")
                continue
            unresolved = project.get("confidence") == "unresolved"
            counts["unresolved" if unresolved else "saved"] += 1
            mark = "~" if unresolved else "+"
            self.stdout.write(f"  {mark} {worker.bot_name}: {worker.title} -> {project.get('name')}")
            if options["dry_run"] or (unresolved and options["skip_unresolved"]):
                continue
            worker.state = {**(worker.state or {}), "project": project}
            worker.save(update_fields=["state", "updated_at"])
        verb = "Would save" if options["dry_run"] else "Saved"
        self.stdout.write(
            f"{verb} {counts['saved']} projects, {counts['unresolved']} unresolved "
            f"(worktree gone); {counts['missing']} workers had no worktree or bot."
        )
