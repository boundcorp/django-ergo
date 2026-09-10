"""Immutable in-memory content access, without filesystem or ORM dependencies."""

from copy import deepcopy
from threading import RLock
from typing import Protocol

from .schema import Snapshot
from .schema import require
from .schema import validate_snapshot


class CorpusBackend(Protocol):
    collection_id: str
    scope: str

    def load(self) -> Snapshot: ...


class WritableCorpusBackend(CorpusBackend, Protocol):
    def commit(
        self, snapshot: Snapshot, *, expected_revision: str, event: dict
    ) -> None: ...

    def operations(self) -> list[dict]: ...


class MemoryCorpus:
    """An isolated virtual corpus; a host owns persistence and revision selection."""

    def __init__(self, snapshot: Snapshot):
        validate_snapshot(snapshot)
        self.collection_id = snapshot.collection_id
        self.scope = snapshot.scope
        self._snapshot = Snapshot.from_dict(snapshot.to_dict())
        self._lock = RLock()
        self._operations = []

    def load(self):
        return self._snapshot

    def commit(self, snapshot, *, expected_revision, event):
        validate_snapshot(snapshot)
        with self._lock:
            require(
                self._snapshot.revision == expected_revision,
                "Corpus changed; rebase and review again",
            )
            require(
                (snapshot.collection_id, snapshot.scope)
                == (self.collection_id, self.scope),
                "Backend identity mismatch",
            )
            self._snapshot = snapshot
            self._operations.append(deepcopy(event))

    def operations(self):
        with self._lock:
            return deepcopy(self._operations)
