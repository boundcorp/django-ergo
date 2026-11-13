"""Supervised one-create H3 controller; fixture mode is entirely offline.

Prepare the ledger before starting the independent cleaner. Live execution is
explicit and requires a named credential environment callback and fresh cleaner.
The injected fixture opener is trusted test code, not a network sandbox.
"""
# The controller shares the ledger's transaction primitives and exposes a CLI.
# ruff: noqa: SLF001, T201

from __future__ import annotations

import argparse
import io
import json
import os
import re
import sqlite3
import time
from decimal import Decimal
from pathlib import Path

from .budget import Evidence
from .budget import RentalTimeline
from .cleanup import RESTCleanup
from .experiment import Experiment
from .supervisor import CleanerSupervision
from .transport import FixtureRunpodREST
from .transport import RunpodREST

GPU = "NVIDIA RTX 6000 Ada Generation"
DISK_GB = 200


def rental_budget(config):
    """Convert reviewed pricing/timeline evidence for the shared admission rule."""
    pricing = config["pricing"]

    def evidence(name):
        item = pricing[name]
        return Evidence(Decimal(item["amount"]), item["verified"])

    return {
        "timeline": RentalTimeline(**config["timeline"]),
        "compute_hourly_usd": evidence("compute_hourly_usd"),
        "storage_usd": evidence("storage_usd"),
        "network_usd": evidence("network_usd"),
        "fees_usd": evidence("fees_usd"),
        "project_cap_usd": Decimal(pricing["project_cap_usd"]),
        "project_committed_usd": Decimal(pricing["project_committed_usd"]),
    }


def prepare(path, config, *, now):
    """Reserve and bind one immutable H3 request before any POST."""
    if config.get("mode") not in ("offline-fixture", "live"):
        msg = "explicit offline-fixture or live mode required"
        raise ValueError(msg)
    if type(config.get("live_execute")) is not bool:
        msg = "explicit live_execute boolean required"
        raise ValueError(msg)
    if config["mode"] == "offline-fixture" and config["live_execute"]:
        msg = "offline fixture cannot enable live execution"
        raise ValueError(msg)
    experiment = Experiment(path)
    experiment.reserve(
        now=now,
        idle_grace=config["idle_grace"],
        eligibility=config["eligibility"],
        **rental_budget(config),
    )
    RESTCleanup(path).configure_create(
        account=config["account"],
        image=config["image"],
        container_disk_gb=DISK_GB,
        gpu_type=GPU,
        data_center_ids=config["data_center_ids"],
        quoted_hourly_usd=config["pricing"]["compute_hourly_usd"]["amount"],
        ssh_public_key=config.get("ssh_public_key"),
    )
    with experiment._tx() as db:
        db.execute(
            "CREATE TABLE h3_create_mode (mode TEXT NOT NULL, live_execute INTEGER NOT NULL)"
        )
        db.execute(
            "INSERT INTO h3_create_mode VALUES (?, ?)",
            (config["mode"], int(config["live_execute"])),
        )
    return experiment.status()


def create_once(path, *, now, opener=None, mode, live_execute, credential_env=None):  # noqa: PLR0913
    """Submit the bound request once; live mode requires the independent cleaner."""
    experiment = Experiment(path)
    with experiment._tx() as db:
        try:
            bound = db.execute(
                "SELECT mode, live_execute FROM h3_create_mode"
            ).fetchone()
        except sqlite3.OperationalError:
            msg = "controller configuration not bound"
            raise ValueError(msg) from None
        if bound is None or (bound["mode"], bound["live_execute"]) != (
            mode,
            int(live_execute is True),
        ):
            msg = "controller mode differs from reserved configuration"
            raise ValueError(msg)
    if (
        mode == "offline-fixture"
        and live_execute is False
        and opener is not None
        and credential_env is None
    ):
        client = FixtureRunpodREST(opener)
    elif (
        mode == "live"
        and live_execute is True
        and opener is None
        and isinstance(credential_env, str)
        and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", credential_env)
    ):
        client = RunpodREST(lambda: os.environ.get(credential_env))
    else:
        msg = "live create disabled without explicit live mode and credential environment name"
        raise ValueError(msg)
    CleanerSupervision(path).require_fresh(now=now)
    return RESTCleanup(path).create_once(
        now=now, client=client, live_execute=live_execute
    )


class _FixtureOpener:
    def __init__(self, result):
        self.result = result

    def open(self, request, timeout):
        if self.result == "timeout":
            raise TimeoutError
        return io.BytesIO(json.dumps({"id": self.result}).encode())


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    prep = sub.add_parser("prepare")
    prep.add_argument("database")
    prep.add_argument("config")
    launch = sub.add_parser("create")
    launch.add_argument("database")
    launch.add_argument("config")
    launch.add_argument("--execute", action="store_true")
    launch.add_argument("--credential-env")
    launch.add_argument("--fixture-result", help="offline fixture Pod ID or timeout")
    args = parser.parse_args(argv)
    config = json.loads(Path(args.config).read_text())
    if args.action == "prepare":
        state = prepare(args.database, config, now=time.time())
    else:
        mode = config.get("mode")
        if mode == "live" and (
            config.get("live_execute") is not True
            or not args.execute
            or args.fixture_result is not None
        ):
            parser.error(
                "live mode requires live_execute=true and --execute, without fixture"
            )
        if mode == "offline-fixture" and (
            config.get("live_execute") is not False
            or args.execute
            or args.fixture_result is None
        ):
            parser.error(
                "offline mode requires live_execute=false and --fixture-result"
            )
        state = create_once(
            args.database,
            now=time.time(),
            opener=_FixtureOpener(args.fixture_result)
            if args.fixture_result is not None
            else None,
            mode=mode,
            live_execute=config.get("live_execute"),
            credential_env=args.credential_env,
        )
    print(
        json.dumps(
            {
                key: state[key]
                for key in ("phase", "create_started", "cleanup_error", "deadline")
            }
        )
    )


if __name__ == "__main__":
    main()
