"""Optional rebuildable vector projection; no ORM, filesystem or SDK dependency."""

import math
from dataclasses import dataclass

from .schema import CorpusError
from .schema import require

FIELDS = frozenset({"content", "summary", "title"})


class CapabilityUnavailableError(CorpusError):
    """A host has not configured the optional capability required by an operation."""


def capability(condition, message):
    if not condition:
        raise CapabilityUnavailableError(message)


def normalized_weights(weights):
    weights = {"content": 0.6, "summary": 0.4} if weights is None else weights
    require(
        isinstance(weights, dict) and bool(weights) and set(weights) <= FIELDS,
        "Unknown or empty search weights",
    )
    require(
        all(
            type(value) in (int, float) and math.isfinite(value) and value >= 0
            for value in weights.values()
        ),
        "Weights must be finite nonnegative numbers",
    )
    total = sum(weights.values())
    require(total > 0, "At least one weight must be positive")
    return {name: value / total for name, value in weights.items() if value}


def vector_values(vector, dimensions):
    require(
        isinstance(vector, (list, tuple)) and len(vector) == dimensions,
        "Vector dimension mismatch",
    )
    require(
        all(type(value) in (float, int) and math.isfinite(value) for value in vector),
        "Vectors must contain finite numbers",
    )
    norm = math.sqrt(sum(value * value for value in vector))
    require(math.isfinite(norm) and norm > 0, "Vector must have finite nonzero norm")
    return tuple(value / norm for value in vector)


@dataclass(frozen=True)
class VectorProjection:
    collection_id: str
    scope: str
    corpus_revision: str
    provider_id: str
    dimensions: int
    vectors: dict


class MemoryVectorIndex:
    """Disposable projection; recreate identically from any authorized snapshot.

    provider_id must identify the host's model/configuration/version, not merely
    the SDK class. The host may attach the same implementation to any backend.
    """

    def __init__(self):
        self.projection = None

    def rebuild(self, snapshot, documents, provider, provider_id):
        require(
            isinstance(provider_id, str) and bool(provider_id.strip()),
            "A stable provider fingerprint is required",
        )
        dimensions = provider.get_dimensions()
        require(
            type(dimensions) is int and dimensions > 0, "Invalid provider dimensions"
        )
        vectors = {}
        for document in documents:
            vectors[document.reference] = {
                name: vector_values(
                    provider.generate_embedding(getattr(document, name)), dimensions
                )
                for name in sorted(FIELDS)
                if getattr(document, name).strip()
            }
        self.projection = VectorProjection(
            snapshot.collection_id,
            snapshot.scope,
            snapshot.revision,
            provider_id,
            dimensions,
            vectors,
        )

    def check(self, snapshot, provider_id):
        projection = self.projection
        capability(
            projection is not None, "Semantic index is not built; call rebuild_index()"
        )
        require(
            (projection.collection_id, projection.scope)
            == (snapshot.collection_id, snapshot.scope),
            "Index scope mismatch",
        )
        capability(
            projection.corpus_revision == snapshot.revision,
            "Semantic index is stale; call rebuild_index()",
        )
        capability(
            projection.provider_id == provider_id,
            "Semantic provider fingerprint changed; rebuild the index",
        )
        return projection

    def scores(self, snapshot, documents, query_vector, weights, provider_id):
        projection = self.check(snapshot, provider_id)
        query = vector_values(query_vector, projection.dimensions)
        weights = normalized_weights(weights)
        results = {}
        for document in documents:
            fields = projection.vectors.get(document.reference)
            require(fields is not None, "Index document revision mismatch")
            available = {
                name: weight for name, weight in weights.items() if name in fields
            }
            if not available:
                continue
            total = sum(available.values())
            results[document.reference] = (
                sum(
                    weight
                    * sum(
                        left * right
                        for left, right in zip(query, fields[name], strict=True)
                    )
                    for name, weight in available.items()
                )
                / total
            )
        return results
