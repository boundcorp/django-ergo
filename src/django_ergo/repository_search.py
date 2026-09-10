"""Bounded hybrid retrieval over one indexed repository source."""

from __future__ import annotations

from collections import defaultdict

from django.contrib.postgres.search import SearchQuery
from django.contrib.postgres.search import SearchRank
from pgvector.django import CosineDistance

from django_ergo.embedding_providers import get_embedding_provider
from django_ergo.models import SourceRelation
from django_ergo.models import SourceUnit


class RepositorySearchError(RuntimeError):
    pass


def _serialize(unit, score, branches, context=None):
    source_file = unit.source_file
    return {
        "unit_id": str(unit.id),
        "score": score,
        "branch_ranks": branches,
        "path": source_file.relative_path,
        "role": source_file.source_role,
        "kind": unit.kind,
        "symbol": unit.qualified_name,
        "start_line": unit.start_line,
        "end_line": unit.end_line,
        "content_hash": unit.content_hash,
        "commit": source_file.last_indexed_commit,
        "evidence": unit.evidence_text,
        "relation_context": context or [],
    }


def search_repository(
    source, query, *, top_k=8, roles=None, kinds=None, provider=None, mode="hybrid"
):
    if mode not in {"lexical", "hybrid", "semantic"}:
        raise RepositorySearchError("Search mode must be lexical, semantic or hybrid.")
    if not isinstance(query, str) or not query.strip() or not 1 <= top_k <= 100:
        raise RepositorySearchError(
            "A nonempty query and top_k between 1 and 100 are required."
        )
    if not source.last_indexed_commit:
        raise RepositorySearchError("Repository source has not been indexed.")
    roles, kinds = roles or [], kinds or []
    base = SourceUnit.objects.filter(source_file__source=source).select_related(
        "source_file"
    )
    if roles:
        base = base.filter(source_file__source_role__in=roles)
    if kinds:
        base = base.filter(kind__in=kinds)
    branch = defaultdict(dict)
    exact = base.filter(qualified_name__icontains=query).order_by("id")[:50]
    for rank, unit in enumerate(exact, 1):
        branch[unit.id]["exact"] = rank
    lexical = (
        base.annotate(rank=SearchRank("search_vector", SearchQuery(query)))
        .filter(rank__gt=0)
        .order_by("-rank", "id")[:50]
    )
    for rank, unit in enumerate(lexical, 1):
        branch[unit.id]["lexical"] = rank
    if mode == "semantic":
        branch.clear()
    if mode != "lexical":
        provider = provider or get_embedding_provider()
        if (
            source.index_embedding_id != provider.get_index_id()
            or provider.get_dimensions() != 1536
        ):
            raise RepositorySearchError(
                "Repository index embedding provider does not match its recorded fingerprint; rebuild explicitly or use mode='lexical'."
            )
        vector = (
            base.exclude(embedding__isnull=True)
            .annotate(
                distance=CosineDistance("embedding", provider.generate_embedding(query))
            )
            .order_by("distance", "id")[:50]
        )
        for rank, unit in enumerate(vector, 1):
            branch[unit.id]["vector"] = rank
    direct_ids = list(branch)[:10]
    neighbors = SourceRelation.objects.filter(
        source=source, from_unit_id__in=direct_ids
    ).select_related("to_unit__source_file")
    contexts = defaultdict(list)
    for relation in neighbors:
        if relation.to_unit_id not in branch:
            branch[relation.to_unit_id]["neighbor"] = 1
        contexts[relation.to_unit_id].append(
            {"type": relation.relation_type, "from_unit_id": str(relation.from_unit_id)}
        )
    weights = {"exact": 2, "lexical": 1, "vector": 1, "neighbor": 0.5}
    scored = [
        (sum(weights[key] / (60 + rank) for key, rank in ranks.items()), unit_id, ranks)
        for unit_id, ranks in branch.items()
    ]
    units = {unit.id: unit for unit in base.filter(id__in=[item[1] for item in scored])}
    return [
        _serialize(units[unit_id], score, ranks, contexts[unit_id])
        for score, unit_id, ranks in sorted(
            scored, key=lambda item: (-item[0], str(item[1]))
        )[:top_k]
        if unit_id in units
    ]
