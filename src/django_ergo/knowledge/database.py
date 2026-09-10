"""Optional no-filesystem snapshot persistence in the host's Django database."""

from .schema import Snapshot
from .schema import require
from .schema import validate_snapshot


def _model(name):
    from django.apps import apps

    return apps.get_model("ergo_knowledge", name)


class DatabaseCorpus:
    def __init__(self, *, collection_id, scope, revision, using="default"):
        self.collection_id = collection_id
        self.scope = scope
        self.revision = revision
        self.using = using

    @classmethod
    def store(cls, snapshot, *, using="default"):
        """Trusted host intake; append/idempotently reuse a validated snapshot."""
        revision_model = _model("CorpusRevision")

        validate_snapshot(snapshot)
        record, _created = revision_model.objects.using(using).get_or_create(
            collection_id=snapshot.collection_id,
            scope=snapshot.scope,
            revision=snapshot.revision,
            defaults={"payload": snapshot.to_dict()},
        )
        require(
            record.payload == snapshot.to_dict(), "Stored revision integrity mismatch"
        )
        return cls(
            collection_id=snapshot.collection_id,
            scope=snapshot.scope,
            revision=snapshot.revision,
            using=using,
        )

    def load(self):
        revision_model = _model("CorpusRevision")

        revision = self.revision
        if revision is None:
            revision = self._head().revision

        record = (
            revision_model.objects.using(self.using)
            .filter(
                collection_id=self.collection_id,
                scope=self.scope,
                revision=revision,
            )
            .first()
        )
        require(record is not None, "Corpus revision is unavailable")
        snapshot = Snapshot.from_dict(record.payload)
        require(snapshot.revision == revision, "Stored revision integrity mismatch")
        return snapshot

    def _head(self):
        return (
            _model("CorpusHead")
            .objects.using(self.using)
            .get(collection_id=self.collection_id, scope=self.scope)
        )

    @classmethod
    def workspace(cls, snapshot, *, using="default"):
        """Initialize a live head if absent; never reset an existing workspace."""
        from django.db import transaction

        with transaction.atomic(using=using):
            backend = cls.store(snapshot, using=using)
            _model("CorpusHead").objects.using(using).get_or_create(
                collection_id=snapshot.collection_id,
                scope=snapshot.scope,
                defaults={"revision": snapshot.revision},
            )
        backend.revision = None
        return backend

    def commit(self, snapshot, *, expected_revision, event):
        from django.db import transaction

        require(
            self.revision is None,
            "Pinned database snapshots are read-only; use workspace()",
        )
        require(
            (snapshot.collection_id, snapshot.scope)
            == (self.collection_id, self.scope),
            "Backend identity mismatch",
        )
        with transaction.atomic(using=self.using):
            head = self._head()
            updated = (
                _model("CorpusHead")
                .objects.using(self.using)
                .filter(pk=head.pk, revision=expected_revision)
                .update(revision=snapshot.revision)
            )
            require(updated == 1, "Corpus changed; rebase and review again")
            self.store(snapshot, using=self.using)
            _model("CorpusOperation").objects.using(self.using).create(
                head=head, payload=event
            )

    def operations(self):
        require(self.revision is None, "Operation log belongs to the live workspace")
        return list(
            self._head()
            .operations.using(self.using)
            .order_by("pk")
            .values_list("payload", flat=True)
        )
