"""Optional POSIX JSONL usage journal; not a filesystem requirement on the KB."""

import json
import os
from contextlib import contextmanager
from pathlib import Path

from .retrieval import CapabilityUnavailableError
from .schema import MAX_CORPUS_BYTES
from .schema import CorpusError
from .schema import require
from .usage import validate_usage_event


class FileUsageStore:
    """A host-owned append-only journal, independently placed from any Git corpus."""

    def __init__(self, path):
        self.path = Path(path)

    @contextmanager
    def _locked(self):
        try:
            import fcntl
        except ImportError as exc:
            message = (
                "FileUsageStore requires POSIX locks; configure another UsageStore"
            )
            raise CapabilityUnavailableError(message) from exc
        descriptor = os.open(
            self.path,
            os.O_CREAT | os.O_RDWR | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        with os.fdopen(descriptor, "a+", encoding="utf-8") as stream:
            fcntl.flock(stream, fcntl.LOCK_EX)
            try:
                stream.seek(0, os.SEEK_END)
                require(
                    stream.tell() <= MAX_CORPUS_BYTES,
                    "Usage journal size limit reached; host rotation is required",
                )
                stream.seek(0)
                try:
                    events = [json.loads(line) for line in stream if line.strip()]
                    for event in events:
                        validate_usage_event(event)
                except (json.JSONDecodeError, CorpusError) as exc:
                    message = "Usage journal is corrupt"
                    raise CorpusError(message) from exc
                yield stream, events
            finally:
                fcntl.flock(stream, fcntl.LOCK_UN)

    def record(self, event):
        validate_usage_event(event)
        with self._locked() as (stream, events):
            key = (event["collection_id"], event["scope"], event["event_id"])
            previous = next(
                (
                    item
                    for item in events
                    if (item["collection_id"], item["scope"], item["event_id"]) == key
                ),
                None,
            )
            if previous is not None:
                require(previous == event, "Usage event identity conflict")
                return
            encoded = json.dumps(event, sort_keys=True) + "\n"
            stream.seek(0, os.SEEK_END)
            require(
                stream.tell() + len(encoded.encode("utf-8")) <= MAX_CORPUS_BYTES,
                "Usage journal size limit reached; host rotation is required",
            )
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())

    def read(self, collection_id, scope, context_id=None):
        with self._locked() as (_stream, events):
            return [
                event
                for event in events
                if event["collection_id"] == collection_id
                and event["scope"] == scope
                and (context_id is None or event["context_id"] == context_id)
            ]
