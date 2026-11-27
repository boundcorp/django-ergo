"""Guarded edits and appends of session text files (``ergo_attachments_edit``/``_append``)."""

from __future__ import annotations

import hashlib
import json
import threading

import pytest
from django.contrib.auth import get_user_model
from django.db import connection

from django_ergo.bots.tools import ToolContext
from django_ergo.conversation.attachments import save_session_file
from django_ergo.conversation.models import ConversationAttachment
from django_ergo.conversation.models import ConversationSession
from django_ergo.conversation.tool_dispatch import ToolError
from tests.test_bot_plugins import files_bot

User = get_user_model()


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@pytest.fixture
def world(tmp_path, settings):
    settings.MEDIA_ROOT = str(tmp_path / "media")
    bot, _, plugin = files_bot(tmp_path, config="max_bytes: 200")
    lee = User.objects.create(username="lee-edit")
    other = User.objects.create(username="other-edit")
    mine = ConversationSession.objects.create(user=lee, bot_name="filer")
    older = ConversationSession.objects.create(user=lee, bot_name="filer")
    theirs = ConversationSession.objects.create(user=other, bot_name="filer")
    ctx = ToolContext(bot=bot, session=mine, user=lee)
    return {
        "plugin": plugin,
        "ctx": ctx,
        "mine": mine,
        "older": older,
        "theirs": theirs,
    }


def error_of(call) -> ToolError:
    with pytest.raises(ToolError) as caught:
        call()
    return caught.value


@pytest.mark.django_db
def test_edit_replaces_one_span_and_returns_receipt(world):
    plugin, ctx = world["plugin"], world["ctx"]
    data = b"alpha\nbeta\ngamma\n"
    row = save_session_file(world["mine"], "n.md", data)
    receipt = plugin.edit(
        ctx, str(row.id), "beta", "BETA\nBETA2", False, sha(data)[:12]
    )
    assert receipt["version"] == 2
    assert receipt["previous_sha256"] == sha(data)
    assert receipt["changed_lines"] == "2-3"
    assert receipt["total_lines"] == 4
    assert receipt["replacements"] == 1
    assert receipt["preview"] == ["2: BETA", "3: BETA2"]
    row.refresh_from_db()
    with row.file.open("rb") as handle:
        stored = handle.read()
    assert stored == b"alpha\nBETA\nBETA2\ngamma\n"
    assert receipt["sha256"] == row.sha256 == sha(stored)
    assert row.size == len(stored)
    # The version marker works as the precondition too.
    again = plugin.edit(ctx, "n.md", "alpha", "A", expected_version=2)
    assert again["version"] == 3


@pytest.mark.django_db
def test_edit_stale_version_changes_nothing(world):
    plugin, ctx = world["plugin"], world["ctx"]
    row = save_session_file(world["mine"], "n.md", b"one\ntwo\n")
    read_sha = row.sha256
    plugin.update(ctx, str(row.id), "one\nTWO\n")  # someone else wrote since
    err = error_of(lambda: plugin.edit(ctx, str(row.id), "one", "1", False, read_sha))
    assert err.category == "stale_version"
    assert err.state_changed == "no"
    assert err.fields["current_sha256"] == sha(b"one\nTWO\n")
    stale_version = error_of(
        lambda: plugin.edit(ctx, str(row.id), "one", "1", expected_version=1)
    )
    assert stale_version.category == "stale_version"
    row.refresh_from_db()
    with row.file.open("rb") as handle:
        assert handle.read() == b"one\nTWO\n"
    assert row.metadata["version"] == 2


@pytest.mark.django_db
def test_edit_detects_external_write_the_row_hash_missed(world):
    """The precondition is compared with the bytes read under the lock."""
    plugin, ctx = world["plugin"], world["ctx"]
    row = save_session_file(world["mine"], "n.md", b"one\n")
    with row.file.open("wb") as handle:  # a write that bypassed the row
        handle.write(b"uno\n")
    err = error_of(lambda: plugin.edit(ctx, str(row.id), "uno", "x", False, row.sha256))
    assert err.category == "stale_version"


@pytest.mark.django_db
def test_edit_needs_a_precondition_and_a_unique_match(world):
    plugin, ctx = world["plugin"], world["ctx"]
    data = b"x\nsame\ny\nsame\nz\nsame\nw\nsame\n"
    row = save_session_file(world["mine"], "n.md", data)
    h = row.sha256
    assert error_of(lambda: plugin.edit(ctx, str(row.id), "x", "y")).category == (
        "invalid_argument"
    )
    ambiguous = error_of(lambda: plugin.edit(ctx, str(row.id), "same", "S", False, h))
    assert ambiguous.category == "ambiguous_match"
    assert ambiguous.fields["matches"] == 4
    assert ambiguous.fields["first_lines"] == [2, 4, 6]
    missing = error_of(lambda: plugin.edit(ctx, str(row.id), "absent", "S", False, h))
    assert missing.category == "not_found"
    assert error_of(lambda: plugin.edit(ctx, str(row.id), "", "S", False, h))
    row.refresh_from_db()
    assert row.sha256 == h and row.metadata.get("version", 1) == 1
    receipt = plugin.edit(ctx, str(row.id), "same", "S", True, h)
    assert receipt["replacements"] == 4
    assert receipt["changed_lines"] == "2-8"


@pytest.mark.django_db
def test_edit_and_append_refuse_other_sessions_and_private_files(world):
    plugin, ctx = world["plugin"], world["ctx"]
    older = save_session_file(world["older"], "o.md", b"old\n")
    theirs = save_session_file(world["theirs"], "t.md", b"nope\n")
    photo = save_session_file(world["mine"], "p.jpg", b"\xff\xd8jpeg")
    with pytest.raises(ValueError, match="read-only"):
        plugin.edit(ctx, str(older.id), "old", "new", False, older.sha256)
    with pytest.raises(ValueError, match="read-only"):
        plugin.append(ctx, str(older.id), "more")
    with pytest.raises(ValueError, match="No file"):
        plugin.edit(ctx, str(theirs.id), "nope", "yes", False, theirs.sha256)
    with pytest.raises(ValueError, match="No file"):
        plugin.append(ctx, str(theirs.id), "more")
    assert error_of(lambda: plugin.append(ctx, str(photo.id), "more")).category == (
        "invalid_argument"
    )
    # The core function itself is scoped to the session it is given.
    from django_ergo.conversation.attachments import append_session_text

    err = error_of(
        lambda: append_session_text(world["mine"].id, theirs.id, "x", max_bytes=100)
    )
    assert err.category == "not_found"
    theirs.refresh_from_db()
    assert theirs.metadata.get("version", 1) == 1


@pytest.mark.django_db
def test_size_limits_refuse_instead_of_truncating(world):
    plugin, ctx = world["plugin"], world["ctx"]
    row = save_session_file(world["mine"], "n.md", b"U" + b"a" * 149)
    err = error_of(
        lambda: plugin.edit(ctx, str(row.id), "U", "b" * 100, False, row.sha256)
    )
    assert err.category == "limit_exceeded"
    err = error_of(lambda: plugin.append(ctx, str(row.id), "c" * 100))
    assert err.category == "limit_exceeded"
    # A larger upload than the write limit can't be edited or appended to.
    big = save_session_file(world["mine"], "big.txt", b"y" * 300)
    for call in (
        lambda: plugin.edit(ctx, str(big.id), "y", "z", False, big.sha256),
        lambda: plugin.append(ctx, str(big.id), "z"),
    ):
        err = error_of(call)
        assert err.category == "limit_exceeded" and "nothing was changed" in str(err)
    # A very long single line is edited by exact text without being cut.
    long_line = b"s" + b"m" * 150 + b"e\n"
    liner = save_session_file(world["mine"], "line.txt", long_line)
    receipt = plugin.edit(ctx, str(liner.id), "e\n", "E\n", False, liner.sha256)
    assert receipt["changed_lines"] == "1-1"
    assert len(receipt["preview"][0]) <= 205
    for row_id in (row.id, big.id):
        assert (
            ConversationAttachment.objects.get(id=row_id).metadata.get("version", 1)
            == 1
        )
    # Non-UTF-8 text is refused rather than rewritten lossy.
    latin = save_session_file(world["mine"], "l.txt", b"caf\xe9\n")
    assert error_of(lambda: plugin.append(ctx, str(latin.id), "x")).category == (
        "invalid_argument"
    )


@pytest.mark.django_db
def test_append_idempotency_replay_and_conflict(world):
    plugin, ctx = world["plugin"], world["ctx"]
    row = save_session_file(world["mine"], "log.md", b"first\n")
    one = plugin.append(ctx, str(row.id), "second", idempotency_key="k1")
    assert one["version"] == 2 and one["changed_lines"] == "2-2"
    assert "replayed" not in one
    replay = plugin.append(ctx, str(row.id), "second", idempotency_key="k1")
    assert replay["replayed"] is True
    assert {k: v for k, v in replay.items() if k != "replayed"} == one
    # Replay wins over a now-stale precondition: the retry has the old hash.
    again = plugin.append(
        ctx,
        str(row.id),
        "second",
        idempotency_key="k1",
        expected_sha256=sha(b"first\n"),
    )
    assert again["replayed"] is True
    err = error_of(
        lambda: plugin.append(ctx, str(row.id), "other text", idempotency_key="k1")
    )
    assert err.category == "conflict" and err.state_changed == "no"
    row.refresh_from_db()
    with row.file.open("rb") as handle:
        assert handle.read() == b"first\nsecond"
    assert row.metadata["version"] == 2
    # Keys are scoped to the file.
    other = save_session_file(world["mine"], "log2.md", b"")
    fresh = plugin.append(ctx, str(other.id), "second", idempotency_key="k1")
    assert "replayed" not in fresh and fresh["changed_lines"] == "1-1"
    # Without a key, each call appends; the separator is not doubled.
    plugin.append(ctx, str(row.id), "third\n")
    plugin.append(ctx, str(row.id), "fourth")
    row.refresh_from_db()
    with row.file.open("rb") as handle:
        assert handle.read() == b"first\nsecond\nthird\nfourth"


@pytest.mark.django_db
def test_append_keys_are_bounded(world):
    from django_ergo.conversation.attachments import APPEND_KEYS_KEPT

    plugin, ctx = world["plugin"], world["ctx"]
    row = save_session_file(world["mine"], "log.md", b"")
    for n in range(APPEND_KEYS_KEPT + 3):
        plugin.append(ctx, str(row.id), "x", separator="", idempotency_key=f"k{n}")
    row.refresh_from_db()
    keys = [k["key"] for k in row.metadata["append_keys"]]
    assert len(keys) == APPEND_KEYS_KEPT and keys[-1] == f"k{APPEND_KEYS_KEPT + 2}"


@pytest.mark.django_db
def test_failed_edit_leaves_no_partial_state_or_orphan_file(world):
    plugin, ctx = world["plugin"], world["ctx"]
    row = save_session_file(world["mine"], "n.md", b"one\n")
    storage = row.file.storage
    before = set(storage.listdir(row.file.name.rsplit("/", 1)[0])[1])
    error_of(lambda: plugin.edit(ctx, str(row.id), "zzz", "y", False, row.sha256))
    error_of(lambda: plugin.append(ctx, str(row.id), "q" * 500))
    after = set(storage.listdir(row.file.name.rsplit("/", 1)[0])[1])
    assert before == after


@pytest.mark.django_db
def test_legacy_update_path_is_unchanged(world):
    plugin, ctx = world["plugin"], world["ctx"]
    made = plugin.create(ctx, "list.md", "- eggs")
    updated = plugin.update(ctx, made["id"], "- eggs\n- milk")
    assert updated["size"] == len("- eggs\n- milk")
    assert updated["version"] == 2
    assert set(updated) == set(made)
    assert plugin.read(ctx, made["id"]).endswith("- eggs\n- milk")
    row = ConversationAttachment.objects.get(id=made["id"])
    assert "append_keys" not in row.metadata
    # Updating after an edit still works, and bumps the same counter.
    plugin.edit(ctx, made["id"], "milk", "oats", False, updated["sha256"])
    assert plugin.update(ctx, made["id"], "x")["version"] == 4


@pytest.mark.django_db(transaction=True)
def test_concurrent_edits_only_one_passes_the_precondition(world):
    plugin, ctx = world["plugin"], world["ctx"]
    row = save_session_file(world["mine"], "race.md", b"start\n")
    start_sha = row.sha256
    barrier = threading.Barrier(2)
    outcomes = []

    def writer(replacement):
        try:
            barrier.wait(timeout=10)
            outcomes.append(
                plugin.edit(ctx, str(row.id), "start", replacement, False, start_sha)
            )
        except ToolError as exc:
            outcomes.append(exc)
        finally:
            connection.close()

    threads = [threading.Thread(target=writer, args=(w,)) for w in ("A", "B")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    wins = [o for o in outcomes if isinstance(o, dict)]
    losses = [o for o in outcomes if isinstance(o, ToolError)]
    assert len(wins) == 1 and len(losses) == 1
    assert losses[0].category == "stale_version"
    row.refresh_from_db()
    assert row.metadata["version"] == 2
    with row.file.open("rb") as handle:
        assert handle.read() in (b"A\n", b"B\n")


@pytest.mark.django_db(transaction=True)
def test_concurrent_keyed_appends_apply_once_and_all_distinct_keys_land(world):
    plugin, ctx = world["plugin"], world["ctx"]
    row = save_session_file(world["mine"], "j.md", b"")
    results = []

    def writer(key, text):
        try:
            results.append(plugin.append(ctx, str(row.id), text, idempotency_key=key))
        finally:
            connection.close()

    jobs = [("same", "dup")] * 3 + [("a", "A"), ("b", "B")]
    threads = [threading.Thread(target=writer, args=job) for job in jobs]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert len(results) == 5
    row.refresh_from_db()
    with row.file.open("rb") as handle:
        lines = handle.read().decode().split("\n")
    assert sorted(lines) == ["A", "B", "dup"]
    assert row.metadata["version"] == 4
    assert sum(1 for r in results if r.get("replayed")) == 2


@pytest.mark.django_db
def test_tools_are_registered_and_receipts_are_json(world):
    plugin, ctx = world["plugin"], world["ctx"]
    toolkit = plugin.toolkits(ctx)[0]
    assert toolkit.has_tool("ergo_attachments_edit")
    assert toolkit.has_tool("ergo_attachments_append")
    row = save_session_file(world["mine"], "n.md", b"a\n")
    result = toolkit.execute_tool(
        "ergo_attachments_append",
        {"attachment_id": str(row.id), "content": "b", "idempotency_key": "k"},
    )
    json.dumps(result)
