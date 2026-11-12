from concurrent.futures import ThreadPoolExecutor

import pytest

from django_ergo.plugins.runpod.queue import Conflict
from django_ergo.plugins.runpod.queue import Queue
from django_ergo.plugins.runpod.queue import StaleLease


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    import socket

    def deny(*args, **kwargs):
        msg = "network forbidden in offline queue tests"
        raise AssertionError(msg)

    monkeypatch.setattr(socket.socket, "connect", deny)
    monkeypatch.setattr(socket, "create_connection", deny)


def append(queue, key, phase, **changes):
    args = {
        "project": "project",
        "request_key": key,
        "shot": key,
        "version": "v1",
        "phase": phase,
        "spec": {"duration": 29 if phase == 0 else 30},
    }
    args.update(changes)
    return queue.append(**args)


def accepted(queue, job, now):
    identity = queue.prepare_submission(job["id"], job["epoch"], now)
    queue.record_receipt(job["id"], identity)
    queue.observe_submission(job["id"], job["epoch"], now, state="accepted")
    queue.finish(job["id"], job["epoch"], now, status="succeeded")


def test_initial_order_correction_and_replenishment(tmp_path):
    queue = Queue(tmp_path / "queue.sqlite")
    ch2 = append(queue, "ch2", 2)
    ch1 = append(queue, "ch1", 1)
    test = append(queue, "test", 0)
    first = queue.claim(now=0, lease_seconds=30)
    assert first["id"] == test["id"]
    correction = append(
        queue,
        "fix",
        1,
        shot="ch1",
        version="v1",
        original_id=ch1["id"],
        reason="approved correction",
    )
    accepted(queue, first, 1)
    second = queue.claim(now=2, lease_seconds=30)
    assert second["id"] == correction["id"]
    accepted(queue, second, 3)
    third = queue.claim(now=4, lease_seconds=30)
    assert third["id"] == ch1["id"]
    accepted(queue, third, 5)
    fourth = queue.claim(now=6, lease_seconds=30)
    assert fourth["id"] == ch2["id"]
    accepted(queue, fourth, 7)
    assert Queue(tmp_path / "queue.sqlite").claim(now=8, lease_seconds=30) is None
    new = append(queue, "new", 2)
    assert queue.claim(now=9, lease_seconds=30)["id"] == new["id"]
    assert queue.jobs()[1]["spec_hash"] == ch1["spec_hash"]


def test_duplicate_append_and_correction_identity(tmp_path):
    queue = Queue(tmp_path / "queue.sqlite")
    original = append(queue, "shot", 1)
    assert append(queue, "shot", 1)["id"] == original["id"]
    with pytest.raises(Conflict):
        append(queue, "shot", 2)
    with pytest.raises(ValueError, match="original shot and version"):
        append(
            queue,
            "fix",
            1,
            shot="shot",
            version="v2",
            original_id=original["id"],
            reason="change",
        )
    with pytest.raises(ValueError, match="same project"):
        append(
            queue,
            "fix",
            1,
            project="other",
            shot="shot",
            original_id=original["id"],
            reason="change",
        )


def test_submission_identity_is_stable_per_database_and_distinct_between_queues(
    tmp_path,
):
    path = tmp_path / "first.sqlite"
    first = append(Queue(path), "shot", 0)
    assert append(Queue(path), "shot", 0)["submission_id"] == first["submission_id"]
    other = append(Queue(tmp_path / "second.sqlite"), "shot", 0)
    assert other["submission_id"] != first["submission_id"]


def test_concurrent_claim_and_lost_receipt_inspection(tmp_path):
    path = tmp_path / "queue.sqlite"
    queue = Queue(path)
    item = append(queue, "shot", 0)
    with ThreadPoolExecutor(max_workers=4) as pool:
        claims = list(
            pool.map(lambda _: Queue(path).claim(now=0, lease_seconds=10), range(4))
        )
    (first,) = [claim for claim in claims if claim is not None]
    identity = queue.prepare_submission(item["id"], first["epoch"], 1)
    # The remote may have accepted the request even though the reply was lost.
    second = queue.claim(now=10, lease_seconds=10)
    assert second["epoch"] == first["epoch"] + 1
    queue.record_receipt(item["id"], identity)  # late receipt survives fencing
    with pytest.raises(Conflict):
        queue.prepare_submission(item["id"], second["epoch"], 11)
    with pytest.raises(StaleLease):
        queue.finish(item["id"], first["epoch"], 11, status="failed")
    queue.observe_submission(item["id"], second["epoch"], 11, state="accepted")
    queue.finish(item["id"], second["epoch"], 12, status="succeeded")
    assert queue.receipts(item["id"]) == [identity]
    assert queue.jobs()[0]["status"] == "succeeded"


def test_cancel_is_terminal_even_for_late_owner(tmp_path):
    queue = Queue(tmp_path / "queue.sqlite")
    item = append(queue, "shot", 0)
    claim = queue.claim(now=0, lease_seconds=30)
    identity = queue.prepare_submission(item["id"], claim["epoch"], 1)
    queue.cancel()
    queue.record_receipt(item["id"], identity)
    with pytest.raises(StaleLease):
        queue.finish(item["id"], claim["epoch"], 2, status="succeeded")
    with pytest.raises(ValueError, match="queue is stopped"):
        append(queue, "another", 1)
    assert queue.claim(now=3, lease_seconds=30) is None
    assert queue.jobs()[0]["status"] == "cancelled"
    assert queue.receipts(item["id"]) == [identity]


def test_correction_priority_stays_within_original_phase(tmp_path):
    queue = Queue(tmp_path / "queue.sqlite")
    chapter = append(queue, "chapter", 1)
    correction = append(
        queue,
        "chapter-fix",
        1,
        shot="chapter",
        original_id=chapter["id"],
        reason="approved correction",
    )
    pilot = append(queue, "pilot", 0)
    first = queue.claim(now=0, lease_seconds=30)
    assert first["id"] == pilot["id"]
    accepted(queue, first, 1)
    assert queue.claim(now=2, lease_seconds=30)["id"] == correction["id"]


def test_correction_cannot_change_original_phase(tmp_path):
    queue = Queue(tmp_path / "queue.sqlite")
    chapter = append(queue, "chapter", 1)
    with pytest.raises(ValueError, match="original phase"):
        append(
            queue,
            "chapter-fix",
            0,
            shot="chapter",
            original_id=chapter["id"],
            reason="must not promote chapter work into pilot",
        )
