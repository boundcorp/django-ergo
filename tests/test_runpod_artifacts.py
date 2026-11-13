from dataclasses import FrozenInstanceError
from hashlib import sha256

import pytest

from django_ergo.plugins.runpod.artifacts import Artifact
from django_ergo.plugins.runpod.artifacts import ManifestItem
from django_ergo.plugins.runpod.artifacts import build_manifest
from django_ergo.plugins.runpod.artifacts import publish_artifact
from django_ergo.plugins.runpod.artifacts import validate_artifact


def artifact(index, name, content):
    return validate_artifact(
        index, name, content, len(content), sha256(content).hexdigest()
    )


def publish(existing, index, name, content):
    return publish_artifact(
        existing, index, name, content, len(content), sha256(content).hexdigest()
    )


def test_manifest_is_stably_ordered_and_immutable():
    z = artifact(3, "z", b"last")
    a = artifact(3, "a", b"first")
    items = [ManifestItem(3, "succeeded", (z, a)), ManifestItem(0, "succeeded")]
    manifest = build_manifest(items)
    assert [item.index for item in manifest.items] == [0, 3]
    assert manifest.items[1].artifacts == (a, z)
    assert manifest == build_manifest(reversed(items))
    assert manifest.is_terminal
    with pytest.raises(FrozenInstanceError):
        manifest.items[1].state = "failed"
    with pytest.raises(ValueError, match="duplicate item"):
        build_manifest([items[0], items[0]])


def test_validation_rejects_digest_and_size_mismatches():
    with pytest.raises(ValueError, match="SHA-256 mismatch"):
        validate_artifact(0, "output", b"data", 4, sha256(b"other").hexdigest())
    with pytest.raises(ValueError, match="size mismatch"):
        validate_artifact(0, "output", b"data", 5, sha256(b"data").hexdigest())
    with pytest.raises(ValueError, match="lowercase"):
        validate_artifact(0, "output", b"data", 4, sha256(b"data").hexdigest().upper())


def test_publication_is_idempotent_and_never_replaces_content():
    published = publish((), 2, "result", b"one")
    assert publish(published, 2, "result", b"one") == published
    with pytest.raises(ValueError, match="already exists"):
        publish(published, 2, "result", b"two")
    assert published == (artifact(2, "result", b"one"),)
    assert publish(published, 0, "other", b"x")[0].index == 0
    with pytest.raises(ValueError, match="SHA-256 mismatch"):
        publish_artifact(published, 2, "result", b"two", 3, published[0].sha256)


def test_partial_failed_and_cancelled_items_remain_visible():
    partial = artifact(1, "partial", b"partial result")
    items = [
        ManifestItem(4, "running"),
        ManifestItem(1, "cancelled", (partial,)),
        ManifestItem(2, "failed"),
        ManifestItem(0, "queued"),
        ManifestItem(3, "succeeded"),
    ]
    manifest = build_manifest(items)
    assert [item.state for item in manifest.items] == [
        "queued",
        "cancelled",
        "failed",
        "succeeded",
        "running",
    ]
    assert manifest.items[1].artifacts == (partial,)
    assert not manifest.is_terminal
    assert build_manifest(items[1:3]).is_terminal


def test_invalid_or_ambiguous_metadata_is_rejected():
    result = artifact(0, "result", b"x")
    with pytest.raises(ValueError, match="belong"):
        ManifestItem(1, "succeeded", (result,))
    with pytest.raises(ValueError, match="duplicate artifact"):
        ManifestItem(0, "succeeded", (result, result))
    with pytest.raises(ValueError, match="index"):
        Artifact(-1, "result", 1, sha256(b"x").hexdigest())
    with pytest.raises(ValueError, match="invalid item state"):
        ManifestItem(0, "unknown")
    other = artifact(0, "result", b"y")
    with pytest.raises(ValueError, match="conflicting existing"):
        publish((result, other), 1, "result", b"z")
