"""Offline-prototyped native intent admission; transcripts are never task arguments.

The raw envelope commits before lifecycle processing. All lifecycle, argument and
receipt changes share the session lock and the normal delegation transaction.
The ledger is deliberately non-evicting: reaching a resource bound disables new
admission rather than forgetting a replay identity.
"""

import copy
import hashlib
import json

from django.db import transaction

from ergonaut.apps.bots.models import RealtimeVoiceSession
from ergonaut.realtime_voice_execution import native_request_capacity

STATE_KEY = "_voice_native"
MAX_RAW_BYTES = 2 * 1024 * 1024
MAX_EVENT_BYTES = 65536
MAX_CONTEXT_INTENTS = 12
KINDS = {"new_request", "clarify", "correct", "cancel", "status", "continue"}
ARG_KEYS = {"kind", "intent_id", "target_intent_id", "request_text", "clarification"}
TERMINAL_EVENTS = {"response.completed", "response.failed", "response.incomplete"}


def _encoded(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True, allow_nan=False)


def _identity(value):
    return isinstance(value, str) and 0 < len(value) <= 160 and value.strip() == value


def _ledger(session):
    return session.state.setdefault(
        STATE_KEY,
        {
            "raw": [],
            "bytes": 0,
            "responses": {},
            "active": {},
            "calls": {},
            "items": {},
            "intents": {},
            "intent_order": [],
            "delegations": {},
        },
    )


def _save(session):
    session.save(update_fields=["state"])


def context_for_session(session):
    """Bounded authoritative task projection, never raw provider payloads."""
    ledger = (session.state or {}).get(STATE_KEY, {})
    # JSONB object key order is not chronological; only the durable array is.
    intents = [
        (intent_id, ledger["intents"][intent_id]) for intent_id in ledger.get("intent_order", [])[-MAX_CONTEXT_INTENTS:]
    ]
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
    return {"intents": projected, "capacity": capacity, "blocked": ledger.get("blocked", "")}


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
    from ergonaut.api.realtime_voice import deadline_expired, voice_allowed

    if session.status != "active" or deadline_expired(session):
        return "Voice session ended or expired; no new work was admitted."
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
            scope = _encoded([claim_id, payload.get("delegation_id")])
            response_scope = (
                _encoded([claim_id, payload.get("delegation_id"), response_id])
                if response_id
                else ledger["active"].get(scope)
            )
        existing = next(
            (
                i
                for i, row in enumerate(ledger["raw"])
                if row["fingerprint"] == fingerprint
                and row["claim_id"] == claim_id
                and row.get("response_scope") == response_scope
            ),
            None,
        )
        if existing is not None:
            index = existing
            cached = ledger["raw"][index].get("receipt")
            if cached is not None:
                return {**copy.deepcopy(cached), "context": context_for_session(session)}
        else:
            size = len(encoded.encode())
            if size > MAX_EVENT_BYTES or ledger["bytes"] + size > MAX_RAW_BYTES:
                ledger["blocked"] = "Native evidence capacity exceeded; this call cannot admit further work."
                # Never truncate a payload and later treat it as admission evidence.
                ledger["overflow"] = {"sha256": fingerprint, "bytes": size, "claim_id": claim_id}
                _save(session)
                return _result(session, "rejected", explanation=ledger["blocked"])
            wire_id = payload.get("event_id")
            if wire_id and any(
                row["payload"].get("event_id") == wire_id and row["fingerprint"] != fingerprint for row in ledger["raw"]
            ):
                ledger["blocked"] = "Native wire event identity was reused with conflicting payloads."
            index = len(ledger["raw"])
            ledger["raw"].append(
                {
                    "payload": payload,
                    "claim_id": claim_id,
                    "response_scope": response_scope,
                    "fingerprint": fingerprint,
                    "receipt": None,
                }
            )
            ledger["bytes"] += size
            _save(session)
    # Separate durable transaction: a failed normal admission cannot erase raw evidence.
    with transaction.atomic(durable=True):
        session = RealtimeVoiceSession.objects.select_for_update().select_related("chat", "user").get(pk=voice_id)
        ledger = _ledger(session)
        cached = ledger["raw"][index].get("receipt")
        if cached is not None:
            return {**copy.deepcopy(cached), "context": context_for_session(session)}
        result = _process(session, ledger, payload, claim_id)
        # delegate() refreshes its own instance; preserve its state updates without
        # replacing the lifecycle ledger this transaction currently owns.
        latest = RealtimeVoiceSession.objects.get(pk=voice_id).state
        session.state = {**latest, STATE_KEY: ledger}
        # Context is a live projection, not replay identity. Avoid multiplying a
        # full task snapshot by every raw streaming event in the durable ledger.
        ledger["raw"][index]["receipt"] = copy.deepcopy(
            {key: value for key, value in result.items() if key != "context"}
        )
        _save(session)
        return result


def _process(session, ledger, envelope, claim_id):
    error = ledger.get("blocked") or _live_error(session, claim_id)
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
    visible_ids = set(ledger.get("intent_order", [])[-MAX_CONTEXT_INTENTS:])
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
    ledger.setdefault("intent_order", []).append(args["intent_id"])
    return receipt
