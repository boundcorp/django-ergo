from django.db import models
from papa.apps.ergo.fields import SummarizedVectorField
from papa.apps.ergo.fields import generate_embedding
from papa.utils.models import TimestampMixin
from pgvector.django import CosineDistance
from pgvector.django import VectorField


class KnowledgeBaseManager(models.Manager):
    def to_markdown(self):
        return "\n\n".join(
            [
                f"# KB ID: {kb.id}\nName: {kb.name}\nDescription: {kb.description}"
                for kb in self.get_queryset()
            ]
        )


class KnowledgeBaseSearchMixin:
    def hybrid_search(self, query_text):
        """
        Perform a hybrid search combining PostgreSQL full-text and embedding similarity.

        Parameters:
        - query_text (str): User's search query
        - top_k (int): Number of results to return

        Returns:
        - QuerySet: Top-k relevant results, balanced between text and semantic search.
        """

        embedding = generate_embedding(query_text)

        return (
            self.get_queryset()
            .annotate(semantic_distance=CosineDistance("embedding", embedding))
            .order_by("semantic_distance")
        )

    def to_prefetch_results(self, top_k=5):
        return [
            {
                "title": article.title,
                "content": article.content,
                "hierarchy_code": article.hierarchy_code,
            }
            for article in self.get_queryset()[:top_k]
        ]


class KnowledgeBaseArticleQuerySet(models.QuerySet, KnowledgeBaseSearchMixin):
    pass


class KnowledgeBaseArticleManager(models.Manager, KnowledgeBaseSearchMixin):
    queryset_class = KnowledgeBaseArticleQuerySet


class Knowledgebase(TimestampMixin):
    """
    A model to store a knowledgebase. A KB is a nested hierarchy of articles.
    """

    name = models.CharField(max_length=255)
    description = models.TextField()
    owner_id = models.CharField(max_length=255, null=True, blank=True, db_index=True)

    def __str__(self):
        return self.name

    def to_markdown(self):
        return f"# KB ID: {self.id}\nName: {self.name}\nDescription: {self.description}"

    def get_table_of_contents(self):
        return "\n".join(
            [
                f"# {article.hierarchy_code} {article.title}"
                for article in self.articles.filter(hierarchy_code__regex=r"^.$")
            ]
        )


class Article(TimestampMixin):
    """
    An article or document in a knowledgebase.

    An article has a title (short synopsis, up to 100 tokens), and a content (longer text, up to 10,000 tokens).

    Each article has a hierarchy code, which is a string of digits and letters that represent the article's position in the knowledgebase (0-indexed hexidecimal).
    For example, the article with hierarchy code "C3" is the 3rd article in the 12th chapter of the knowledgebase.
    We can retrieve full articles by their hierarchy code, search for articles by semantic similarity, or request an index of articles in a given chapter.
    For example, if you retrieve the index for "" (empty string), you will get the titles of articles "0" - "F" (0th through 15th articles).
    Each index takes about 1600 tokens, so you can browse indexes much faster than full articles.
    """

    knowledgebase = models.ForeignKey(
        Knowledgebase, on_delete=models.CASCADE, related_name="articles"
    )
    hierarchy_code = models.CharField(
        max_length=16,
        db_index=True,
        default="0",
        help_text="The hierarchy code of the article, e.g. '012' (0th chapter, 1st section, 2nd sub-section) or 'C3' (12th chapter, 3rd sub-section)",
    )
    title = models.CharField(max_length=512)
    content = SummarizedVectorField()
    embedding = VectorField(dimensions=1536, null=True, editable=False)
    summary = models.TextField(null=True, editable=False)

    objects = KnowledgeBaseArticleManager()

    def __str__(self):
        return self.title
