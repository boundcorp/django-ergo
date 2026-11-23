from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Event

import pytest
from django.db import close_old_connections
from django.utils import timezone

from django_ergo.bots import admission_drain as drain
from django_ergo.conversation.models import AdmissionDrain

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture(autouse=True)
def singleton():
    AdmissionDrain.objects.get_or_create(pk=1)


def test_expiry_recovery_and_owner_fencing():
    ttl = timedelta(seconds=30)
    first = drain.acquire(ttl, ttl)
    assert first and drain.acquire(ttl, ttl) is None
    assert drain.admit(lambda: 1) == (False, None)
    AdmissionDrain.objects.filter(pk=1).update(
        expires_at=timezone.now() - timedelta(seconds=1)
    )
    assert not drain.renew(first, ttl)
    assert drain.admit(lambda: 1) == (True, 1)
    second = drain.acquire(ttl, ttl)
    assert second and second != first
    assert not drain.release(first)
    assert drain.admit(lambda: 1) == (False, None)
    assert drain.release(second)
    assert drain.admit(lambda: 2) == (True, 2)


def test_admission_and_lease_serialize():
    entered, finish = Event(), Event()

    def claim():
        close_old_connections()
        try:

            def register():
                entered.set()
                assert finish.wait(5)
                return "registered"

            return drain.admit(register)
        finally:
            close_old_connections()

    def acquire():
        close_old_connections()
        try:
            return drain.acquire(timedelta(seconds=30), timedelta(seconds=60))
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as executor:
        claim_future = executor.submit(claim)
        assert entered.wait(5)
        lease_future = executor.submit(acquire)
        assert not lease_future.done()
        finish.set()
        assert claim_future.result(timeout=5) == (True, "registered")
        token = lease_future.result(timeout=5)
    assert token
    assert drain.admit(lambda: pytest.fail("claim raced active lease")) == (False, None)
    assert drain.release(token)


def test_renewal_cannot_extend_past_fixed_deadline():
    token = drain.acquire(timedelta(seconds=30), timedelta(seconds=60))
    before = AdmissionDrain.objects.get(pk=1)
    assert drain.renew(token, timedelta(days=1))
    after = AdmissionDrain.objects.get(pk=1)
    assert after.deadline == before.deadline
    assert after.expires_at == before.deadline


def test_failed_registration_rolls_back():
    def fail():
        AdmissionDrain.objects.filter(pk=1).update(
            owner="00000000-0000-0000-0000-000000000001"
        )
        raise RuntimeError("rollback")  # noqa: EM101

    with pytest.raises(RuntimeError, match="rollback"):
        drain.admit(fail)
    assert AdmissionDrain.objects.get(pk=1).owner is None
