"""Record the project (git repository) of coding-agent workers that have none yet.

    ergonaut manage bots_agent_projects [--limit 500]

Agents started since projects were recorded carry theirs; running ones look
theirs up on their next check. This fills in finished ones from the worktree
path they kept, so the Costs page can group them by project.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Record the git repository of agent workers that have none yet"

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=500)

    def handle(self, *args, limit: int, **options):
        from django_ergo.bots import webhooks
        from django_ergo.bots.agents import fill_projects

        filled = fill_projects(webhooks.get_registry(), limit=limit)
        self.stdout.write(f"Recorded the project of {filled} agent workers.")
