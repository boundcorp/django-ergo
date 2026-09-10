"""Optional database persistence for logical snapshots, without vector fields."""

from django.db import models


class CorpusRevision(models.Model):
    collection_id = models.CharField(max_length=255)
    scope = models.CharField(max_length=255)
    revision = models.CharField(max_length=64)
    payload = models.JSONField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["collection_id", "scope", "revision"],
                name="ergo_corpus_revision_unique",
            ),
        ]

    def __str__(self):
        return f"{self.collection_id}@{self.revision}"


class CorpusHead(models.Model):
    collection_id = models.CharField(max_length=255)
    scope = models.CharField(max_length=255)
    revision = models.CharField(max_length=64)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["collection_id", "scope"], name="ergo_corpus_head_unique"
            )
        ]

    def __str__(self):
        return f"{self.collection_id}:{self.scope}"


class CorpusOperation(models.Model):
    head = models.ForeignKey(
        CorpusHead, on_delete=models.PROTECT, related_name="operations"
    )
    payload = models.JSONField()
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return str(self.pk)


class CorpusUsage(models.Model):
    collection_id = models.CharField(max_length=255)
    scope = models.CharField(max_length=255)
    context_id = models.CharField(max_length=255, blank=True)
    event_id = models.CharField(max_length=64)
    payload = models.JSONField()

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["collection_id", "scope", "event_id"],
                name="ergo_corpus_usage_unique",
            )
        ]
        indexes = [
            models.Index(
                fields=["collection_id", "scope", "context_id"],
                name="ergo_corpus_usage_context",
            )
        ]

    def __str__(self):
        return self.event_id
