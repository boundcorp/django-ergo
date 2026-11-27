# ruff: noqa: TRY003, EM101, PLR2004
"""Bounded envelopes and shared limits; keys stay in the trusted app/controller."""

import hashlib
import hmac
import json
import time
import uuid

MAX_REQUEST = 96 * 1024
MAX_RESPONSE = 96 * 1024
CHUNK_BYTES = 16 * 1024
MAX_FILE_BYTES = 64 * 1024 * 1024


class ProtocolError(ValueError):
    """Invalid or unauthenticated broker message."""


def encode(value, limit=MAX_REQUEST):
    data = json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()
    if len(data) > limit:
        raise ProtocolError("Envelope exceeds byte limit")
    return data


def identifier(value):
    try:
        if not isinstance(value, str) or str(uuid.UUID(value)) != value:
            raise ProtocolError("Invalid identifier")
    except (ValueError, AttributeError) as exc:
        raise ProtocolError("Invalid identifier") from exc
    return value


def sign(key, data):
    if not isinstance(key, bytes) or len(key) < 32:
        raise ProtocolError("Controller key must have at least 32 bytes")
    return hmac.new(key, data, hashlib.sha256).hexdigest()


def seal(key, body, *, now=None):
    message = {
        "v": 1,
        "nonce": str(uuid.uuid4()),
        "at": int(time.time() if now is None else now),
        "body": body,
    }
    return encode({"message": message, "mac": sign(key, encode(message))})


def open_envelope(key, raw, *, now=None):
    if len(raw) > MAX_RESPONSE:
        raise ProtocolError("Envelope exceeds byte limit")
    try:
        envelope = json.loads(raw)
        if set(envelope) != {"message", "mac"}:
            raise ProtocolError("Malformed envelope")
        message = envelope["message"]
        if set(message) != {"v", "nonce", "at", "body"} or message["v"] != 1:
            raise ProtocolError("Invalid protocol version")
        identifier(message["nonce"])
        if (
            type(message["at"]) is not int
            or abs((time.time() if now is None else now) - message["at"]) > 60
        ):
            raise ProtocolError("Expired envelope")
        if not hmac.compare_digest(sign(key, encode(message)), envelope["mac"]):
            raise ProtocolError("Unauthenticated envelope")
        return message
    except (
        KeyError,
        TypeError,
        UnicodeDecodeError,
        RecursionError,
        json.JSONDecodeError,
    ) as exc:
        raise ProtocolError("Malformed envelope") from exc
