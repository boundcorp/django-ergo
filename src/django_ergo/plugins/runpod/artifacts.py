"""Storage-independent artifact metadata and deterministic queue manifests.

Publication only produces metadata: the caller must persist the bytes and metadata
atomically (or otherwise serialize competing publications) in its own storage.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from hashlib import sha256
from typing import Literal

ItemState = Literal["queued", "running", "succeeded", "failed", "cancelled"]
_STATES = frozenset(("queued", "running", "succeeded", "failed", "cancelled"))
_TERMINAL = frozenset(("succeeded", "failed", "cancelled"))
_HEX = frozenset("0123456789abcdef")
_SHA256_HEX_LENGTH = 64


def _check_index(index: int) -> None:
    if type(index) is not int or index < 0:
        msg = "index must be a non-negative integer"
        raise ValueError(msg)


@dataclass(frozen=True)
class Artifact:
    """Content identity for one named artifact of an indexed item."""

    index: int
    name: str
    size: int
    sha256: str

    def __post_init__(self) -> None:
        _check_index(self.index)
        if not isinstance(self.name, str) or not self.name:
            msg = "artifact name must be a non-empty string"
            raise ValueError(msg)
        if type(self.size) is not int or self.size < 0:
            msg = "artifact size must be a non-negative integer"
            raise ValueError(msg)
        if (
            not isinstance(self.sha256, str)
            or len(self.sha256) != _SHA256_HEX_LENGTH
            or not set(self.sha256) <= _HEX
        ):
            msg = "sha256 must be a lowercase SHA-256 hex digest"
            raise ValueError(msg)


@dataclass(frozen=True)
class ManifestItem:
    index: int
    state: ItemState
    artifacts: tuple[Artifact, ...] = ()

    def __post_init__(self) -> None:
        _check_index(self.index)
        if self.state not in _STATES:
            msg = "invalid item state"
            raise ValueError(msg)
        if not isinstance(self.artifacts, tuple):
            msg = "artifacts must be a tuple"
            raise TypeError(msg)
        names = set()
        for artifact in self.artifacts:
            if not isinstance(artifact, Artifact) or artifact.index != self.index:
                msg = "artifact must belong to its manifest item"
                raise ValueError(msg)
            if artifact.name in names:
                msg = "duplicate artifact name"
                raise ValueError(msg)
            names.add(artifact.name)


@dataclass(frozen=True)
class Manifest:
    items: tuple[ManifestItem, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.items, tuple) or not all(
            isinstance(item, ManifestItem) for item in self.items
        ):
            msg = "items must be a tuple of manifest items"
            raise ValueError(msg)

    @property
    def is_terminal(self) -> bool:
        """Whether every represented item has reached a terminal state."""
        return all(item.state in _TERMINAL for item in self.items)


def validate_artifact(
    index: int, name: str, content: bytes, expected_size: int, expected_sha256: str
) -> Artifact:
    """Validate bytes against declared size and digest and return their metadata."""
    artifact = Artifact(index, name, expected_size, expected_sha256)
    if not isinstance(content, bytes):
        msg = "artifact content must be bytes"
        raise TypeError(msg)
    if len(content) != artifact.size:
        msg = "artifact size mismatch"
        raise ValueError(msg)
    if sha256(content).hexdigest() != artifact.sha256:
        msg = "artifact SHA-256 mismatch"
        raise ValueError(msg)
    return artifact


def publish_artifact(  # noqa: PLR0913
    existing: Iterable[Artifact],
    index: int,
    name: str,
    content: bytes,
    expected_size: int,
    expected_sha256: str,
) -> tuple[Artifact, ...]:
    """Return updated metadata, rejecting replacement of an existing artifact.

    The identity is (item index, name). Repeating identical validated content is
    idempotent. The returned tuple is sorted independently of publication order.
    """
    artifact = validate_artifact(index, name, content, expected_size, expected_sha256)
    by_key: dict[tuple[int, str], Artifact] = {}
    for current in existing:
        if not isinstance(current, Artifact):
            msg = "existing metadata must contain artifacts"
            raise TypeError(msg)
        key = (current.index, current.name)
        if key in by_key and by_key[key] != current:
            msg = "conflicting existing artifact metadata"
            raise ValueError(msg)
        by_key[key] = current
    key = (index, name)
    if key in by_key and by_key[key] != artifact:
        msg = "artifact already exists with different content"
        raise ValueError(msg)
    by_key[key] = artifact
    return tuple(by_key[key] for key in sorted(by_key))


def build_manifest(items: Iterable[ManifestItem]) -> Manifest:
    """Snapshot all supplied items in index order, including unfinished items.

    Callers supply every known item, including items without artifacts; omitted
    items cannot be inferred from the input. Failed and cancelled items retain
    any artifacts already published for them.
    """
    by_index: dict[int, ManifestItem] = {}
    for item in items:
        if not isinstance(item, ManifestItem):
            msg = "items must contain manifest items"
            raise TypeError(msg)
        if item.index in by_index:
            msg = "duplicate item index"
            raise ValueError(msg)
        by_index[item.index] = ManifestItem(
            item.index, item.state, tuple(sorted(item.artifacts, key=lambda a: a.name))
        )
    return Manifest(tuple(by_index[index] for index in sorted(by_index)))
