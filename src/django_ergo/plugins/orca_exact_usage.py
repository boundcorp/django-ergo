"""Exact per-dispatch agent usage from the interval Orca attests in worker-show."""

from __future__ import annotations

import re

EXACT_SOURCE = "orca_exact"
EXACT_BASIS = "fresh_session_exclusive_interval"
EXACT_AGENTS = ("claude", "codex", "omp")
_SHA256 = re.compile(r"[0-9a-f]{64}")


def exact_interval(shown: dict, dispatch: str, agent: str) -> dict | None:
    """The closed interval Orca recorded for ``dispatch``, or None.

    None covers every case where exactness is not attested: an older Orca without
    the field, resumed/shared/changed/remote sessions, or a malformed record.
    """
    identity = shown.get("nativeUsageIdentity")
    if (
        agent not in EXACT_AGENTS
        or not isinstance(identity, dict)
        or (shown.get("dispatch") or {}).get("id") != dispatch
        or identity.get("status") != "observed"
        or identity.get("agent") != agent
    ):
        return None
    session_id = identity.get("sessionId")
    interval = identity.get("usageInterval")
    if (
        not isinstance(session_id, str)
        or not session_id
        or not isinstance(interval, dict)
    ):
        return None
    path = interval.get("transcriptPath")
    file = interval.get("file")
    end = interval.get("endOffset")
    digest = interval.get("sha256")
    if (
        interval.get("status") != "closed"
        or interval.get("basis") != EXACT_BASIS
        or interval.get("startOffset") != 0
        or not isinstance(path, str)
        or session_id not in path.rsplit("/", 1)[-1]
        or not isinstance(file, dict)
        or not isinstance(file.get("ino"), int)
        or isinstance(file.get("ino"), bool)
        or not isinstance(end, int)
        or isinstance(end, bool)
        or end < 0
        or not isinstance(digest, str)
        or not _SHA256.fullmatch(digest)
    ):
        return None
    return {
        "session_id": session_id,
        "path": path,
        "end": end,
        "sha256": digest,
        "inode": file["ino"],
    }
