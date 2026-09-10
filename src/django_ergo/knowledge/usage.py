"""Scope-keyed usage sinks are independent of canonical KB storage."""

from copy import deepcopy
from threading import RLock
from typing import Protocol

from .schema import CorpusError
from .schema import require


class UsageRecordingError(CorpusError):
    def __init__(self, *, committed):
        self.committed = committed
        super().__init__(
            "Corpus publication succeeded but usage recording failed; inspect operations before retrying"
            if committed
            else "Usage recording failed"
        )


def validate_usage_event(event):
    require(
        isinstance(event, dict)
        and set(event)
        == {
            "format",
            "event_id",
            "collection_id",
            "scope",
            "corpus_revision",
            "mode",
            "context_id",
            "actor",
            "captured_at",
            "references",
        },
        "Invalid usage record",
    )
    require(
        event["format"] == "ergo-usage/v1"
        and all(isinstance(event[name], str) for name in event if name != "references"),
        "Invalid usage fields",
    )
    require(
        isinstance(event["references"], list)
        and all(
            isinstance(reference, dict)
            and set(reference) == {"document_id", "revision", "digest"}
            for reference in event["references"]
        ),
        "Invalid usage references",
    )


class UsageStore(Protocol):
    def record(self, event: dict) -> None: ...

    def read(self, collection_id: str, scope: str, context_id=None) -> list[dict]: ...


class MemoryUsageStore:
    """Explicitly process-local; share a sink across services to share history."""

    def __init__(self):
        self._events = {}
        self._lock = RLock()

    def record(self, event):
        validate_usage_event(event)
        key = (event["collection_id"], event["scope"], event["event_id"])
        with self._lock:
            previous = self._events.setdefault(key, deepcopy(event))
            require(previous == event, "Usage event identity conflict")

    def read(self, collection_id, scope, context_id=None):
        with self._lock:
            return [
                deepcopy(event)
                for event in self._events.values()
                if event["collection_id"] == collection_id
                and event["scope"] == scope
                and (context_id is None or event["context_id"] == context_id)
            ]


class DatabaseUsageStore:
    """Optional durable Django sink, also usable with memory or Git corpora."""

    def __init__(self, *, using="default"):
        self.using = using

    def record(self, event):
        from .database import _model

        validate_usage_event(event)
        record, _created = (
            _model("CorpusUsage")
            .objects.using(self.using)
            .get_or_create(
                collection_id=event["collection_id"],
                scope=event["scope"],
                event_id=event["event_id"],
                defaults={"context_id": event["context_id"], "payload": event},
            )
        )
        require(record.payload == event, "Usage event identity conflict")

    def read(self, collection_id, scope, context_id=None):
        from .database import _model

        records = (
            _model("CorpusUsage")
            .objects.using(self.using)
            .filter(collection_id=collection_id, scope=scope)
        )
        if context_id is not None:
            records = records.filter(context_id=context_id)
        return list(records.order_by("pk").values_list("payload", flat=True))
