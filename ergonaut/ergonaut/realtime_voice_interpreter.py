"""Offline-prototyped native intent admission; transcripts are never task arguments.

The raw envelope commits before lifecycle processing. All lifecycle, argument and
receipt changes share the session lock and the normal delegation transaction.
Indexed identity rows retain exact replay evidence without loading or rewriting
the archive. Only the current envelope and twelve recent intents enter memory.
"""

import copy
import hashlib
import json

from django.db import transaction

from ergonaut.apps.bots.models import RealtimeVoiceLedgerEntry, RealtimeVoiceSession
from ergonaut.realtime_voice_execution import native_request_capacity

MAX_EVENT_BYTES = 65536
MAX_RECORD_BYTES = 4 * MAX_EVENT_BYTES
MAX_RESPONSE_CALLS = 2
MAX_CONTEXT_INTENTS = 12
KINDS = {"new_request", "clarify", "correct", "cancel", "status", "continue"}
ARG_KEYS = {"kind", "intent_id", "target_intent_id", "request_text", "clarification"}
TERMINAL_EVENTS = {"response.completed", "response.failed", "response.incomplete"}


def _encoded(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True, allow_nan=False)


def _identity(value):
    return isinstance(value, str) and 0 < len(value) <= 160 and value.strip() == value


class _Records:
    """Lazy identity lookups with one operation's mutable working set."""

    def __init__(self, session, namespace):
        self.session = session
        self.namespace = namespace
        self.loaded = {}
        self.original = {}

    def get(self, identity, default=None):
        if identity is None:
            return default
        if identity not in self.loaded:
            row = RealtimeVoiceLedgerEntry.objects.filter(
                session=self.session,
                namespace=self.namespace,
                key_sha256=hashlib.sha256(identity.encode()).hexdigest(),
            ).first()
            if row is not None and row.identity != identity:
                raise ValueError("Native ledger identity digest collision.")
            self.loaded[identity] = row.data if row is not None else None
            self.original[identity] = copy.deepcopy(self.loaded[identity])
        value = self.loaded[identity]
        return default if value is None else value

    def __getitem__(self, identity):
        value = self.get(identity)
        if value is None:
            raise KeyError(identity)
        return value

    def __setitem__(self, identity, value):
        self.get(identity)
        self.loaded[identity] = value

    def flush(self):
        for identity, data in self.loaded.items():
            if data == self.original[identity]:
                continue
            if len(_encoded(data).encode()) > MAX_RECORD_BYTES:
                raise ValueError("Native identity payload exceeds its individual evidence bound.")
            pending = (
                self.namespace == "delegations" and not data.get("conflict") and not data.get("initial_response_id")
            ) or (
                self.namespace == "responses"
                and data.get("_active", False)
                and (
                    data["state"] == "open"
                    or ((data.get("terminal") or {}).get("type") == "response.completed" and bool(data.get("calls")))
                )
            )
            RealtimeVoiceLedgerEntry.objects.update_or_create(
                session=self.session,
                namespace=self.namespace,
                key_sha256=hashlib.sha256(identity.encode()).hexdigest(),
                defaults={"identity": identity, "data": data, "pending": pending},
            )
            self.original[identity] = copy.deepcopy(data)


class _Ledger(dict):
    def __init__(self, session):
        super().__init__(
            (namespace, _Records(session, namespace))
            for namespace in ("responses", "active", "calls", "items", "intents", "delegations", "wire")
        )

    def flush(self):
        for records in self.values():
            records.flush()


def _ledger(session):
    if not hasattr(session, "_native_ledger"):
        session._native_ledger = _Ledger(session)
    return session._native_ledger


def _recent_intents(session):
    return list(
        reversed(
            list(
                session.native_ledger.filter(namespace="intents")
                .order_by("-id")
                .values_list("identity", "data")[:MAX_CONTEXT_INTENTS]
            )
        )
    )


def context_for_session(session):
    """Bounded authoritative task projection, never raw provider payloads."""
    _ledger(session).flush()
    intents = _recent_intents(session)
    messages = {
        row.event_id: row.message
        for row in session.delegations.select_related("message").filter(
            event_id__in=[record.get("receipt", {}).get("event_id") for _, record in intents]
        )
    }
    projected = []
    for intent_id, record in intents:
        receipt = record["receipt"]
        message = messages.get(receipt.get("event_id"))
        projected.append(
            {
                "intent_id": intent_id,
                "status": message.status if message else record.get("state", receipt["status"]),
                "request_text": record["args"]["request_text"][:1000],
                "clarification": receipt.get("clarification", "")[:500],
                "target_intent_id": record["args"]["target_intent_id"],
                "event_id": receipt.get("event_id"),
                "message_id": receipt.get("message_id"),
                "delegation_id": receipt.get("delegation_id"),
                "result": message.reply_text[:1500] if message else "",
                "error": message.error[:500] if message else "",
            }
        )
    try:
        capacity = native_request_capacity(session)
    except ValueError:
        capacity = {"authorized": False, "max_inflight": 4, "remaining": 0}
    interpretation_pending = session.native_ledger.filter(pending=True).exists()
    return {
        "intents": projected,
        "capacity": capacity,
        "blocked": "",
        "interpretation_pending": interpretation_pending,
    }


def native_context(voice_id):
    return context_for_session(RealtimeVoiceSession.objects.select_related("chat", "user").get(pk=voice_id))


def _result(session, status, receipts=None, explanation=""):
    return {
        "status": status,
        "receipts": receipts or {},
        "clarification": explanation,
        "context": context_for_session(session),
    }


def _live_error(session, claim_id):
    from ergonaut.api.realtime_voice import bridge_lease_expired, voice_allowed

    if session.state.get("voice_mode", "native") != "native":
        return "Native interpreter events cannot admit work in a text-agent voice session."
    if session.status != "active" or bridge_lease_expired(session):
        return "Voice session ended or its bridge disconnected; no new work was admitted."
    if not voice_allowed(session.user):
        return "Voice authorization is disabled; no new work was admitted."
    if session.bridge_claim_id and claim_id != session.bridge_claim_id:
        return "This native event belongs to a stale bridge claim."
    if session.standby_state not in ("live", "requested"):
        return "The voice bridge is not live."
    return ""


def process_native_event(voice_id, event, *, claim_id=None):
    """Commit exact raw evidence, then process one full native envelope in wire order.

    ``receipts`` is keyed by SDK call_id. Only terminal response events return
    admitted receipts. A durable replay returns the original receipt verbatim.
    Call outside an enclosing transaction: raw evidence must survive admission
    rollback. The bridge uses sync_to_async(thread_sensitive=True).
    """
    if not isinstance(event, dict) or event.get("type") not in ("response.event", "session.delegation.created"):
        return {"status": "ignored", "receipts": {}, "context": {"intents": []}}
    payload = copy.deepcopy(event)
    encoded = _encoded(payload)
    fingerprint = hashlib.sha256(encoded.encode()).hexdigest()
    with transaction.atomic(durable=True):
        session = RealtimeVoiceSession.objects.select_for_update().select_related("chat", "user").get(pk=voice_id)
        ledger = _ledger(session)
        inner = payload.get("event")
        response_scope = None
        if payload["type"] == "response.event" and isinstance(inner, dict):
            snapshot = inner.get("response")
            response_id = snapshot.get("id") if isinstance(snapshot, dict) else None
            response_id = response_id or inner.get("response_id")
            response_id = response_id if _identity(response_id) else None
            delegation_id = payload.get("delegation_id")
            delegation_id = delegation_id if _identity(delegation_id) else None
            scope = _encoded([claim_id, delegation_id])
            response_scope = (
                _encoded([claim_id, delegation_id, response_id]) if response_id else ledger["active"].get(scope)
            )
        raw_identity = _encoded([claim_id, response_scope, fingerprint])
        raw_key = hashlib.sha256(raw_identity.encode()).hexdigest()
        raw = RealtimeVoiceLedgerEntry.objects.filter(session=session, namespace="raw", key_sha256=raw_key).first()
        if raw is not None:
            if raw.identity != raw_identity:
                raise ValueError("Native raw identity digest collision.")
            cached = raw.data.get("receipt")
            if cached is not None:
                return {**copy.deepcopy(cached), "context": context_for_session(session)}
        else:
            wire_id = payload.get("event_id")
            conflict = False
            if _identity(wire_id):
                previous = ledger["wire"].get(wire_id)
                conflict = previous is not None and previous["fingerprint"] != fingerprint
                if previous is None:
                    ledger["wire"][wire_id] = {"fingerprint": fingerprint}
            size = len(encoded.encode())
            if size > MAX_EVENT_BYTES:
                # Retain the identity and denial, not a truncated payload that
                # might later be mistaken for complete admission evidence.
                _invalidate_delegation(ledger, payload)
                result = _result(session, "rejected", explanation="Native event exceeds the individual evidence bound.")
                RealtimeVoiceLedgerEntry.objects.create(
                    session=session,
                    namespace="raw",
                    key_sha256=raw_key,
                    identity=raw_identity,
                    data={
                        "sha256": fingerprint,
                        "bytes": size,
                        "claim_id": claim_id,
                        "receipt": {key: value for key, value in result.items() if key != "context"},
                    },
                )
                return result
            raw = RealtimeVoiceLedgerEntry.objects.create(
                session=session,
                namespace="raw",
                key_sha256=raw_key,
                identity=raw_identity,
                data={"payload": payload, "claim_id": claim_id, "conflict": conflict, "receipt": None},
            )
            ledger.flush()
        raw_id = raw.pk
    # Separate durable transaction: a failed normal admission cannot erase raw evidence.
    with transaction.atomic(durable=True):
        session = RealtimeVoiceSession.objects.select_for_update().select_related("chat", "user").get(pk=voice_id)
        ledger = _ledger(session)
        raw = RealtimeVoiceLedgerEntry.objects.get(pk=raw_id)
        cached = raw.data.get("receipt")
        if cached is not None:
            return {**copy.deepcopy(cached), "context": context_for_session(session)}
        result = _process(session, ledger, payload, claim_id, wire_conflict=raw.data.get("conflict", False))
        ledger.flush()
        # Context is a live projection, not part of the immutable replay receipt.
        raw.data["receipt"] = copy.deepcopy({key: value for key, value in result.items() if key != "context"})
        if len(_encoded(raw.data).encode()) > MAX_RECORD_BYTES:
            raise ValueError("Native receipt exceeds its individual evidence bound.")
        raw.save(update_fields=["data"])
        return result


def _invalidate_delegation(ledger, envelope):
    """Incomplete/conflicting evidence closes only its own native envelope."""
    delegation_id = envelope.get("delegation_id")
    if envelope["type"] == "session.delegation.created":
        payload = envelope.get("delegation")
        delegation_id = payload.get("id") if isinstance(payload, dict) else None
    if _identity(delegation_id):
        record = ledger["delegations"].get(delegation_id)
        if record is None:
            ledger["delegations"][delegation_id] = {"payload": None, "intent_id": None, "conflict": True}
        else:
            record["conflict"] = True


def _process(session, ledger, envelope, claim_id, *, wire_conflict=False):
    error = _live_error(session, claim_id)
    if wire_conflict:
        error = "Native wire event identity was reused with conflicting payloads."
        _invalidate_delegation(ledger, envelope)
        return _result(session, "rejected", explanation=error)
    if not _identity(envelope.get("event_id")):
        error = error or "Native envelope is missing a valid server event identity."
    if envelope["type"] == "session.delegation.created":
        delegation = envelope.get("delegation") or {}
        if not isinstance(delegation, dict):
            return _result(session, "rejected", explanation="Malformed native delegation payload.")
        delegation_id = delegation.get("id")
        if not _identity(delegation_id):
            return _result(session, "rejected", explanation="Missing native delegation identity.")
        if "response_id" in delegation and not _identity(delegation["response_id"]):
            error = error or "Invalid delegation response identity."
        if delegation.get("target") != "responses":
            error = error or "Only a responses delegation can supply native function intents."
        old = ledger["delegations"].get(delegation_id)
        if old and old["payload"] != delegation:
            old["conflict"] = True
            error = "Conflicting native delegation identity."
        elif old is None:
            ledger["delegations"][delegation_id] = {"payload": delegation, "intent_id": None}
        if error:
            ledger["delegations"][delegation_id]["conflict"] = True
        return _result(session, "rejected" if error else "staged", explanation=error)
    delegation_id = envelope.get("delegation_id")
    inner = envelope.get("event")
    if not _identity(delegation_id) or not isinstance(inner, dict):
        return _result(session, "rejected", explanation="Missing native delegation or event identity.")
    delegation = ledger["delegations"].get(delegation_id)
    if not delegation or delegation.get("conflict"):
        error = error or "Native function requires an observed, valid responses delegation."
    scope = _encoded([claim_id, delegation_id])
    kind = inner.get("type")
    if not isinstance(kind, str):
        return _result(session, "rejected", explanation="Malformed native response event type.")
    snapshot = inner.get("response") or {}
    if not isinstance(snapshot, dict):
        return _result(session, "rejected", explanation="Malformed native response snapshot.")
    explicit_id = snapshot.get("id") or inner.get("response_id")
    if explicit_id is not None and not _identity(explicit_id):
        return _result(session, "rejected", explanation="Invalid native response identity.")
    if kind == "response.created":
        if not _identity(explicit_id):
            return _result(
                session, "rejected", explanation="response.created requires an authoritative response identity."
            )
        response_key = _encoded([claim_id, delegation_id, explicit_id])
        if delegation is not None and not delegation.get("initial_response_id"):
            expected_id = (delegation.get("payload") or {}).get("response_id")
            if expected_id is not None and expected_id != explicit_id:
                delegation["conflict"] = True
                error = error or "Initial response identity does not match the native delegation."
            delegation["initial_response_id"] = explicit_id
        previous = ledger["active"].get(scope)
        if previous and previous != response_key:
            interrupted = ledger["responses"][previous]
            interrupted["_active"] = False
            if interrupted["state"] == "open":
                interrupted["state"] = "interrupted"
                interrupted["error"] = "A new response replaced an unfinished response."
                error = error or "A continuation arrived before the prior response reached a terminal event."
        response = ledger["responses"].get(response_key)
        if response is None:
            response = {
                "response_id": explicit_id,
                "created": copy.deepcopy(inner),
                "state": "open",
                "calls": {},
                "error": error,
            }
            ledger["responses"][response_key] = response
        elif response["created"] != inner:
            response["error"] = "Conflicting response.created identity."
        response["_active"] = True
        ledger["active"][scope] = response_key
        return _result(
            session, "rejected" if response.get("error") else "staged", explanation=response.get("error", "")
        )
    response_key = ledger["active"].get(scope)
    if explicit_id:
        response_key = _encoded([claim_id, delegation_id, explicit_id])
    response = ledger["responses"].get(response_key)
    if response is None:
        orphan_key = ledger["active"].get(scope)
        orphan = ledger["responses"].get(orphan_key)
        if orphan and orphan["created"] is None:
            response_key, response = orphan_key, orphan
    if response is None:
        # Retain orphan calls for a terminal rejection receipt, but never upgrade
        # their missing created identity into admission authority.
        response_key = response_key or _encoded([claim_id, delegation_id, None])
        response = {
            "response_id": explicit_id,
            "created": None,
            "state": "open",
            "calls": {},
            "error": "Orphan native event without response.created; no work was admitted.",
        }
        ledger["responses"][response_key] = response
        previous = ledger["active"].get(scope)
        if previous and previous != response_key:
            interrupted = ledger["responses"][previous]
            interrupted["_active"] = False
            if interrupted["state"] == "open":
                interrupted["state"] = "interrupted"
                interrupted["error"] = "An orphan response replaced an unfinished response."
        response["_active"] = True
        ledger["active"][scope] = response_key
    if explicit_id and explicit_id != response["response_id"]:
        response["error"] = "Native response identity mismatch."
    if error:
        response["error"] = error
    if kind == "response.output_item.done":
        item = inner.get("item") or {}
        if not isinstance(item, dict):
            response["error"] = "Malformed native output item."
            return _result(session, "rejected", explanation=response["error"])
        if item.get("type") != "function_call":
            return _result(session, "staged")
        call_id, item_id = item.get("call_id"), item.get("id")
        if not _identity(call_id) or not _identity(item_id):
            response["error"] = "Completed function is missing call/item identity."
            return _result(session, "rejected", explanation=response["error"])
        for table, identity in (("calls", call_id), ("items", item_id)):
            old = ledger[table].get(identity)
            signature = {"response": response_key, "item": item}
            if old is not None and old != signature:
                response["error"] = "Function call/item identity was reused with conflicting payload or response."
            else:
                ledger[table][identity] = copy.deepcopy(signature)
        old_item = response["calls"].get(call_id)
        if old_item is not None and old_item != item:
            response["error"] = "Conflicting function argument payload."
        elif response["state"] != "open" and old_item is None:
            response["error"] = "Function arrived after the response was closed."
        elif old_item is None and (
            len(response["calls"]) >= MAX_RESPONSE_CALLS
            or len(_encoded(response["calls"]).encode()) + len(_encoded(item).encode()) > MAX_EVENT_BYTES
        ):
            response["error"] = "Native response has too many pending function arguments; no work was admitted."
        else:
            response["calls"][call_id] = copy.deepcopy(item)
        return _result(
            session, "rejected" if response.get("error") else "staged", explanation=response.get("error", "")
        )
    if kind not in TERMINAL_EVENTS:
        return _result(session, "staged")
    if response.get("receipts") is not None:
        if response.get("terminal") != inner:
            return _result(session, "rejected", explanation="Conflicting response terminal payload.")
        return _result(session, "replayed", response["receipts"])
    if response["state"] != "open":
        error = error or response.get("error") or "Response was interrupted or closed."
    if (
        kind != "response.completed"
        or snapshot.get("status", "completed") != "completed"
        or snapshot.get("error")
        or snapshot.get("incomplete_details")
    ):
        error = error or "Native response did not complete successfully."
    error = error or response.get("error", "")
    if len(response["calls"]) > 1:
        error = error or "Multiple function calls in one native response are ambiguous; no work was admitted."
        if delegation is not None:
            delegation["conflict"] = True
    receipts = {}
    for call_id, item in response["calls"].items():
        identity = {
            "voice_session_id": str(session.pk),
            "claim_id": claim_id,
            "delegation_id": delegation_id,
            "response_id": response["response_id"],
            "call_id": call_id,
            "item_id": item["id"],
        }
        receipts[call_id] = _admit(session, ledger, item, identity, error)
    response["state"] = "completed" if not error else "rejected"
    response["terminal"] = copy.deepcopy(inner)
    response["receipts"] = copy.deepcopy(receipts)
    return _result(session, "rejected" if error else "completed", receipts, error)


def _arguments(item):
    if item.get("name") != "ergo_voice_intent" or item.get("status") != "completed":
        raise ValueError("Only a completed ergo_voice_intent function can be admitted.")
    raw = item.get("arguments")
    if not isinstance(raw, str) or len(raw) > 12000:
        raise ValueError("Function arguments must be a bounded JSON string.")

    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate function argument key.")
            result[key] = value
        return result

    try:
        args = json.loads(raw, object_pairs_hook=unique)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("Function arguments are not complete JSON.") from exc
    if not isinstance(args, dict) or set(args) != ARG_KEYS:
        raise ValueError("Function arguments must contain exactly the required native intent fields.")
    if not isinstance(args["kind"], str) or args["kind"] not in KINDS or not _identity(args["intent_id"]):
        raise ValueError("Invalid native intent kind or identity.")
    if args["target_intent_id"] is not None and not _identity(args["target_intent_id"]):
        raise ValueError("Invalid target intent identity.")
    if any(not isinstance(args[key], str) or len(args[key]) > 4000 for key in ("request_text", "clarification")):
        raise ValueError("Request text and clarification must be bounded strings.")
    if args["kind"] == "new_request" and (args["target_intent_id"] is not None or not args["request_text"].strip()):
        raise ValueError("A new request needs explicit request text and no target intent.")
    return args


def _admit(session, ledger, item, identity, error):
    receipt = {**identity, "status": "rejected", "intent_id": None, "target_intent_id": None, "clarification": error}
    receipt["arguments_sha256"] = (
        hashlib.sha256(item["arguments"].encode()).hexdigest() if isinstance(item.get("arguments"), str) else None
    )
    try:
        args = _arguments(item)
    except ValueError as exc:
        receipt["clarification"] = str(exc)
        return receipt
    receipt.update(intent_id=args["intent_id"], target_intent_id=args["target_intent_id"])
    if error:
        return receipt
    delegation = ledger["delegations"][identity["delegation_id"]]
    selected = delegation.get("selection")
    if selected is None:
        delegation["selection"] = {"intent_id": args["intent_id"], "kind": args["kind"]}
    old = ledger["intents"].get(args["intent_id"])
    if old:
        if old["args"] == args and old["arguments"] == item["arguments"]:
            return copy.deepcopy(old["receipt"])
        receipt["clarification"] = (
            "Intent identity already has different arguments; choose an authoritative existing target or clarify."
        )
        return receipt
    kind, target_id = args["kind"], args["target_intent_id"]
    visible_ids = {intent_id for intent_id, _ in _recent_intents(session)}
    target = ledger["intents"].get(target_id) if target_id in visible_ids else None
    if kind != "new_request" and (kind != "clarify" or target_id is not None) and target is None:
        receipt.update(
            status="clarification",
            clarification="No authoritative target intent matches that ID; ask which existing intent is meant.",
        )
    elif kind == "new_request":
        delegation = ledger["delegations"][identity["delegation_id"]]
        if (
            delegation.get("conflict")
            or delegation.get("intent_id")
            or (selected is not None and selected["intent_id"] != args["intent_id"])
            or delegation.get("initial_response_id") != identity["response_id"]
        ):
            receipt["clarification"] = (
                "This delegation already selected an intent; continuation or result reading cannot start another request."
            )
        else:
            from ergonaut.api.realtime_voice import delegate

            delegation["intent_id"] = args["intent_id"]
            event_id = "native:" + args["intent_id"]
            # Keep admission in a savepoint, so failures retain a durable denied receipt.
            try:
                with transaction.atomic():
                    message = delegate(
                        session.pk,
                        event_id,
                        args["request_text"],
                        native_identity={**identity, "intent_id": args["intent_id"]},
                    )
            except ValueError as exc:
                receipt["clarification"] = str(exc)
            except Exception:  # noqa: BLE001 - normal admission failures must retain raw evidence and fail closed
                receipt["clarification"] = (
                    "Normal thread admission was denied or unavailable; no new work was admitted. Check owner, bot, session, cost authorization and in-flight capacity."
                )
            else:
                receipt.update(
                    status="submitted",
                    message_id=str(message.pk),
                    event_id=event_id,
                    clarification="Accepted through the originating thread's normal tools and action approvals.",
                )
    elif kind == "clarify":
        receipt.update(
            status="clarification",
            clarification=args["clarification"] or "Please clarify the intended request before starting work.",
        )
    elif kind in ("correct", "cancel"):
        receipt["clarification"] = (
            "Cannot safely revise or cancel already submitted thread work from voice. The existing task is unchanged; use its normal thread controls and approvals."
        )
    else:
        receipt.update(
            status="observed",
            clarification="Observed the authoritative target only; continuation, status and result reading do not start work.",
        )
    ledger["intents"][args["intent_id"]] = {
        "args": args,
        "arguments": item["arguments"],
        "receipt": copy.deepcopy(receipt),
        "state": "awaiting_clarification" if kind == "clarify" else receipt["status"],
    }
    return receipt
