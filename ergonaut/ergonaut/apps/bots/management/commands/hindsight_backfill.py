"""Dry-run export of chat turns for a Hindsight backfill. Never calls Hindsight.

    ergonaut manage hindsight_backfill --scope named --out ~/hindsight-backfill

``--scope named`` is the permanent named chats (``bot_role == "chat"``),
``top`` adds each bot's main chat, ``all`` adds threads. Writes one JSONL file
of retain items per chat plus ``summary.json`` under ``--out`` (keep it outside
the repo: it holds chat content). Banks must be supplied explicitly via
``--bank bot=bank``; a bot with no bank, or the protected
``personal`` bank, is listed but exported nowhere.
"""

from __future__ import annotations

import json
from pathlib import Path

from django.core.management.base import BaseCommand

PROTECTED_BANKS = {"personal"}


class Command(BaseCommand):
    help = "Export chat turns as Hindsight retain items (dry run, no network)."
    requires_system_checks = []

    def add_arguments(self, parser):
        parser.add_argument("--scope", choices=["named", "top", "all"], default="named")
        parser.add_argument("--out", required=True)
        parser.add_argument("--bank", action="append", default=[], help="bot=bank")
        parser.add_argument("--host", default="ergo.octo.boundcorp.net")
        parser.add_argument("--no-write", action="store_true", help="count only")

    def handle(self, *args, **opts):
        from django_ergo.conversation.models import ConversationSession
        from django_ergo.plugins import hindsight_backfill as hb

        banks = dict(b.split("=", 1) for b in opts["bank"])
        roles = {"named": ["chat"], "top": ["chat", "main"], "all": ["chat", "main", "thread"]}[opts["scope"]]
        out = Path(opts["out"]).expanduser()
        if not opts["no_write"]:
            out.mkdir(parents=True, exist_ok=True)
        sessions = ConversationSession.objects.filter(metadata__bot_role__in=roles).order_by("bot_name", "created_at")
        summary = []
        for session in sessions:
            bank = banks.get(session.bot_name, "")
            turns = list(hb.turns(session))
            est = hb.estimate(turns)
            role = (session.metadata or {}).get("bot_role")
            messages = session.messages.count()
            dates = session.messages.order_by("sequence")
            first = dates.first()
            last = dates.last()
            row = {
                "session": str(session.id),
                "bot": session.bot_name,
                "role": role,
                "title": session.user_title or (session.metadata or {}).get("title") or "",
                "bank": bank or None,
                "messages": messages,
                "first": first.created_at.date().isoformat() if first else None,
                "last": last.created_at.date().isoformat() if last else None,
                **est,
            }
            summary.append(row)
            if opts["no_write"] or not bank or bank in PROTECTED_BANKS or not est["documents"]:
                continue
            path = out / f"{session.bot_name}-{role}-{session.id}.jsonl"
            with path.open("w") as fh:
                for turn in turns:
                    if not turn.skipped:
                        item = hb.build_item(session.bot_name, session, turn, host=opts["host"])
                        fh.write(json.dumps(item) + "\n")
            path.chmod(0o600)
        if not opts["no_write"]:
            (out / "summary.json").write_text(json.dumps(summary, indent=1))
        self.stdout.write(json.dumps(summary, indent=1))
