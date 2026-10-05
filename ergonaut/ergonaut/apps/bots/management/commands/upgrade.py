"""Upgrade Ergonaut to the newest release (see ergonaut/upgrades).

ergonaut upgrade --check    # report only
ergonaut upgrade            # wait for idle, then run ERGONAUT_UPGRADER
ergonaut upgrade --status   # the last attempt
"""

from __future__ import annotations

import json

from django.core.management.base import BaseCommand

from ergonaut import upgrades


class Command(BaseCommand):
    help = "Upgrade Ergonaut to the newest GitHub release once no turn or worker is running."

    def add_arguments(self, parser):
        parser.add_argument("--check", action="store_true", help="only report whether an upgrade is available")
        parser.add_argument("--status", action="store_true", help="show the last upgrade attempt")
        parser.add_argument(
            "--force", action="store_true", help="upgrade even if the release isn't ahead, or failed recently"
        )
        parser.add_argument("--timeout", type=float, default=30 * 60, help="seconds to wait for idle")
        parser.add_argument("--quiet-for", type=float, default=20)
        parser.add_argument(
            "--ignore-workers",
            action="store_true",
            help="wait for turns only (the default follows ERGONAUT_UPGRADE_WAIT_FOR_WORKERS)",
        )

    def handle(self, *args, check, status, force, timeout, quiet_for, ignore_workers, **options):
        if status:
            self.stdout.write(json.dumps(upgrades.load_state(), indent=2))
            return
        if check:
            self.stdout.write(upgrades.check(upgrades.load_upgrader()).describe())
            return
        self.stdout.write(
            upgrades.run(
                force=force,
                wait_timeout=timeout,
                quiet_for=quiet_for,
                workers=False if ignore_workers else None,
                log=self.stdout.write,
            )
        )
