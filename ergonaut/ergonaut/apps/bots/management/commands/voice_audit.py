"""Export owner-scoped voice metadata; never load transcripts or dispatch work."""

import csv
import hashlib
import json
import re
from datetime import UTC, datetime

from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction
from django.db.models import F
from django.utils.dateparse import parse_datetime
from django_ergo.conversation.models import MessageBlock, StructuredCall

from ergonaut.apps.bots.models import RealtimeVoiceSession

TOKEN_FIELDS = (
    "input_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
    "output_tokens",
    "reasoning_tokens",
)
EVENT_KINDS = frozenset(
    (
        "user",
        "assistant",
        "delegation_signal",
        "background_status",
        "result",
        "task_status",
        "client_audio",
        "bridge_audio",
        "bridge_input",
        "delegation_status",
    )
)


def reference(value):
    return hashlib.sha256(str(value).encode()).hexdigest()[:16]


def label(value):
    # Only machine labels, never arbitrary metadata/error text or CSV formulas.
    return value if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_./:-]{0,99}", value) else "UNKNOWN"


def instant(value):
    return value.astimezone(UTC).isoformat() if value else None


def collect(owner, since=None, until=None):
    """One repeatable READ ONLY PostgreSQL snapshot. No registry/model/tool loading.

    Times filter creation, [since, until). Null is UNKNOWN, never a measured zero.
    Call token fields are stored aggregates (not an audio/text breakdown or invoice).
    """
    if connection.vendor != "postgresql":
        raise CommandError("voice_audit requires PostgreSQL READ ONLY transactions.")
    if connection.in_atomic_block:
        raise CommandError("voice_audit requires its own top-level read-only transaction.")
    with transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        sessions = RealtimeVoiceSession.objects.filter(user__username=owner, chat__user_id=F("user_id")).order_by(
            "created_at", "id"
        )
        if since:
            sessions = sessions.filter(created_at__gte=since)
        if until:
            sessions = sessions.filter(created_at__lt=until)
        rows = []
        for session in sessions.only(
            "id",
            "chat_id",
            "status",
            "created_at",
            "bridge_started_at",
            "ended_at",
            "standby_state",
            "standby_at",
            "resumed_at",
        ).iterator():
            events = []
            for event in session.events.order_by("created_at", "pk").values(
                "kind", "delivery", "created_at", "received_at", "processed_at"
            ):
                events.append(
                    {
                        "kind": event["kind"] if event["kind"] in EVENT_KINDS else "other",
                        "delivery": label(event["delivery"]),
                        **{key: instant(event[key]) for key in ("created_at", "received_at", "processed_at")},
                    }
                )
            delegations = []
            for delegation in session.delegations.filter(message__recipient_session__user__username=owner).values(
                "message_id", "created_at", "message__status", "message__updated_at", "message__recipient_session_id"
            ):
                calls = []
                for call in StructuredCall.objects.filter(
                    session_id=delegation["message__recipient_session_id"],
                    metadata__thread_message=str(delegation["message_id"]),
                ).values(
                    "created_at",
                    "updated_at",
                    "status",
                    "engine_type",
                    "model_name",
                    "turns_used",
                    "first_sequence",
                    "last_sequence",
                    "cost_usd",
                    *TOKEN_FIELDS,
                ):
                    tools = []
                    if call["first_sequence"] is not None and call["last_sequence"] is not None:
                        blocks = MessageBlock.objects.filter(
                            message__session_id=delegation["message__recipient_session_id"],
                            message__sequence__gte=call["first_sequence"],
                            message__sequence__lte=call["last_sequence"],
                            block_type__in=("tool_use", "tool_result"),
                        ).values("block_type", "tool_name", "is_error")
                        tools = [
                            {"kind": b["block_type"], "name": label(b["tool_name"]), "is_error": b["is_error"]}
                            for b in blocks
                        ]
                    calls.append(
                        {
                            "created_at": instant(call["created_at"]),
                            "updated_at": instant(call["updated_at"]),
                            "status": label(call["status"]),
                            "stored_engine_type": label(call["engine_type"]),
                            "recorded_model": label(call["model_name"]),
                            "turns_used": call["turns_used"],
                            "recorded_aggregate_tokens": {key: call[key] for key in TOKEN_FIELDS},
                            "audio_text_token_breakdown": None,
                            "recorded_cost_estimate_usd": str(call["cost_usd"])
                            if call["cost_usd"] is not None
                            else None,
                            "actual_billed_usd": None,
                            "tools": tools,
                        }
                    )
                delegations.append(
                    {
                        "created_at": instant(delegation["created_at"]),
                        "status": label(delegation["message__status"]),
                        "updated_at": instant(delegation["message__updated_at"]),
                        "calls": calls,
                    }
                )
            rows.append(
                {
                    "session_ref": reference(session.pk),
                    "thread_ref": reference(session.chat_id),
                    "created_at": instant(session.created_at),
                    "bridge_started_at": instant(session.bridge_started_at),
                    "ended_at": instant(session.ended_at),
                    "status": label(session.status),
                    "created_to_ended_seconds": (session.ended_at - session.created_at).total_seconds()
                    if session.ended_at
                    else None,
                    "standby_state": label(session.standby_state),
                    "standby_at": instant(session.standby_at),
                    "resumed_at": instant(session.resumed_at),
                    "events": events,
                    "delegations": delegations,
                    "historical_voice_model": None,
                    "historical_provider_prompt": None,
                    "provider_usage_seconds": None,
                    "voice_audio_text_tokens": None,
                    "livekit_metered_usage": None,
                    "invoice_charged_usd": None,
                }
            )
        return {
            "schema_version": 1,
            "observed_at": instant(datetime.now(UTC)),
            "source": "owner-scoped PostgreSQL READ ONLY snapshot",
            "window": {"created_since_inclusive": instant(since), "created_until_exclusive": instant(until)},
            "unknown": "null; zero stored call tokens may be ledger defaults, not proof of zero provider use",
            "sessions": rows,
        }


def render(report, stream, format="json"):
    if format == "json":
        stream.write(json.dumps(report, indent=2) + "\n")
    else:
        fields = [
            "session_ref",
            "thread_ref",
            "created_at",
            "bridge_started_at",
            "ended_at",
            "status",
            "created_to_ended_seconds",
            "standby_state",
            "standby_at",
            "resumed_at",
            "events",
            "delegations",
            "historical_voice_model",
            "historical_provider_prompt",
            "provider_usage_seconds",
            "voice_audio_text_tokens",
            "livekit_metered_usage",
            "invoice_charged_usd",
        ]
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for row in report["sessions"]:
            writer.writerow(
                {
                    key: json.dumps(value) if isinstance(value, dict | list) else "UNKNOWN" if value is None else value
                    for key, value in row.items()
                }
            )


class Command(BaseCommand):
    help = "READ ONLY sanitized voice metadata for one exact owner, with optional UTC creation window."
    requires_system_checks = []

    def add_arguments(self, parser):
        parser.add_argument("--owner", required=True)
        parser.add_argument("--since", help="Timezone-aware ISO creation lower bound, inclusive.")
        parser.add_argument("--until", help="Timezone-aware ISO creation upper bound, exclusive.")
        parser.add_argument("--format", choices=("json", "csv"), default="json")

    def handle(self, *args, **options):
        bounds = []
        for key in ("since", "until"):
            try:
                value = parse_datetime(options[key]) if options.get(key) else None
            except ValueError:
                raise CommandError("Use timezone-aware ISO --since/--until.") from None
            if options.get(key) and (value is None or value.utcoffset() is None):
                raise CommandError("Use timezone-aware ISO --since/--until.")
            bounds.append(value)
        if all(bounds) and bounds[0] >= bounds[1]:
            raise CommandError("--since must precede --until.")
        render(collect(options["owner"], *bounds), self.stdout, options["format"])
