"""Explicit reviewed import and transactional publish bridge for legacy Article callers."""

from uuid import UUID
from uuid import uuid4
from uuid import uuid5

from .schema import Document
from .schema import digest
from .schema import import_snapshot
from .schema import require
from .service import AccessDeniedError


class ArticleCompatibility:
    """A host-authorized bridge, not an implicit second authoring authority.

    authorize(principal, knowledgebase, action) must authorize the separate legacy
    source/destination. Import yields a proposal, never invented approval. Publish
    copies one accepted snapshot to Articles and does not mutate that snapshot.
    Existing Article ORM/QuerySet/toolkit callers keep their types and behavior.
    """

    def __init__(self, service, knowledgebase, authorize):
        self.service = service
        self.knowledgebase = knowledgebase
        self.authorize = authorize
        self._expected_source_revision = None

    def _authorize(self, action):
        if (
            self.authorize(self.service.principal, self.knowledgebase, action)
            is not True
        ):
            message = "Legacy knowledgebase access denied"
            raise AccessDeniedError(message)

    def _fingerprint(self, articles, strategy):
        return digest(
            {
                "knowledgebase": str(self.knowledgebase.pk),
                "strategy": strategy,
                "articles": [
                    {
                        "id": str(article.pk),
                        "title": article.title,
                        "content": article.content,
                        "summary": article.summary,
                        "hierarchy_code": article.hierarchy_code,
                        "status": self._status(article),
                        "relative_path": article.relative_path,
                    }
                    for article in articles
                ],
            }
        )

    @staticmethod
    def _status(article):
        from django.core.exceptions import ObjectDoesNotExist

        try:
            source = article.source_document
        except ObjectDoesNotExist:
            return article.status
        if source.state == "missing":
            return "stale"
        if source.state == "archived":
            return "archived"
        return article.status if source.status == "current" else source.status

    def source_revision(self):
        self._authorize("export")
        self.knowledgebase.refresh_from_db(fields=["organization_strategy"])
        return self._fingerprint(
            self.knowledgebase.articles.order_by("pk"),
            self.knowledgebase.organization_strategy,
        )

    def propose_import(self, *, provenance, reason):
        self._authorize("export")
        base = import_snapshot(self.service.export())
        self.knowledgebase.refresh_from_db(fields=["organization_strategy"])
        articles = list(self.knowledgebase.articles.order_by("pk"))
        self._expected_source_revision = self._fingerprint(
            articles, self.knowledgebase.organization_strategy
        )
        changes = []
        for article in articles:
            evidence = Document(
                f"article-capture:{article.pk}:{uuid4()}",
                str(uuid4()),
                base.scope,
                "evidence",
                article.title,
                article.content,
                "active",
                provenance,
            )
            page = Document(
                str(article.pk),
                str(uuid4()),
                base.scope,
                "page",
                article.title,
                article.content,
                self._status(article),
                provenance,
                (evidence.reference,),
                summary=article.summary or "",
                hierarchy_code=article.hierarchy_code or "",
            )
            changes.extend((evidence, page))
        previous = next(
            (document for document in base.documents if document.kind == "strategy"),
            None,
        )
        if self.knowledgebase.organization_strategy or previous is not None:
            changes.append(
                Document(
                    previous.document_id if previous else "ergo:strategy",
                    str(uuid4()),
                    base.scope,
                    "strategy",
                    "Organization strategy",
                    self.knowledgebase.organization_strategy,
                    "active",
                    provenance,
                )
            )
        proposal = self.service.propose(changes, reason=reason)
        require(
            proposal.base.revision == base.revision,
            "Corpus changed; repeat Article capture",
        )
        return proposal

    def publish(self, *, expected_source_revision=None):
        """Upsert heads; preserve missing rows and UUIDs; never delete Articles."""
        from django.db import transaction

        self._authorize("publish")
        expected_source_revision = (
            expected_source_revision or self._expected_source_revision
        )
        require(
            expected_source_revision is not None,
            "Article publication requires an expected source_revision()",
        )
        snapshot = import_snapshot(self.service.export())
        records = {
            (document.document_id, document.revision): document
            for document in snapshot.documents
        }
        using = self.knowledgebase._state.db or "default"
        article_model = self.knowledgebase.articles.model
        identifiers = {}
        with transaction.atomic(using=using):
            self.knowledgebase.__class__.objects.using(using).select_for_update().get(
                pk=self.knowledgebase.pk
            )
            self.knowledgebase.refresh_from_db(fields=["organization_strategy"])
            articles = list(
                self.knowledgebase.articles.select_for_update().order_by("pk")
            )
            require(
                self._fingerprint(articles, self.knowledgebase.organization_strategy)
                == expected_source_revision,
                "Articles changed; reimport and review before publishing",
            )
            for head in snapshot.heads:
                document = records[(head.document_id, head.revision)]
                if document.kind == "strategy":
                    self.knowledgebase.organization_strategy = (
                        document.content if document.status == "active" else ""
                    )
                    self.knowledgebase.save(
                        using=using, update_fields=["organization_strategy"]
                    )
                if document.kind != "page":
                    continue
                require(
                    bool(document.hierarchy_code),
                    "Article publication requires a hierarchy code",
                )
                try:
                    identifier = UUID(document.document_id)
                except ValueError:
                    identifier = uuid5(
                        self.knowledgebase.pk,
                        f"{snapshot.collection_id}:{snapshot.scope}:{document.document_id}",
                    )
                require(
                    not article_model.objects.using(using)
                    .filter(pk=identifier)
                    .exclude(knowledgebase=self.knowledgebase)
                    .exists(),
                    "Article identity belongs to another knowledgebase",
                )
                require(
                    not self.knowledgebase.articles.filter(
                        hierarchy_code=document.hierarchy_code
                    )
                    .exclude(pk=identifier)
                    .exists(),
                    "Article hierarchy conflicts with an existing identity",
                )
                article_model.objects.using(using).update_or_create(
                    pk=identifier,
                    defaults={
                        "knowledgebase": self.knowledgebase,
                        "title": document.title,
                        "content": document.content,
                        "summary": document.summary,
                        "hierarchy_code": document.hierarchy_code,
                        "status": document.status,
                    },
                )
                identifiers[document.document_id] = str(identifier)
            self._expected_source_revision = self._fingerprint(
                self.knowledgebase.articles.order_by("pk"),
                self.knowledgebase.organization_strategy,
            )
        return {
            "corpus_revision": snapshot.revision,
            "article_ids": identifiers,
            "source_revision": self._expected_source_revision,
        }
