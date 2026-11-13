from uuid import uuid4

import pytest
from django.db import IntegrityError
from django.db import transaction
from django.utils import timezone

from django_ergo.models import LifecycleEvent

pytestmark = pytest.mark.django_db


def event(**kwargs):
    return LifecycleEvent(
        key=kwargs.pop("key", str(uuid4())),
        stage=kwargs.pop("stage", "started"),
        occurred_at=timezone.now(),
        **kwargs,
    )


def test_new_instance_with_existing_primary_key_cannot_overwrite():
    original = event()
    original.save()
    replacement = event(id=original.pk, key=original.key, stage="finished")

    with pytest.raises(IntegrityError), transaction.atomic():
        replacement.save()

    assert LifecycleEvent.objects.get(pk=original.pk).stage == "started"


@pytest.mark.parametrize(
    ("args", "kwargs"),
    [
        ((), {"force_update": True}),
        ((), {"update_fields": ["stage"]}),
        ((), {"update_fields": []}),
        ((False, True), {}),
        ((False, False, None, ["stage"]), {}),
    ],
)
def test_new_instance_rejects_update_options(args, kwargs):
    original = event()
    original.save()
    replacement = event(id=original.pk, key=original.key, stage="finished")

    with pytest.raises(TypeError, match="immutable"):
        replacement.save(*args, **kwargs)

    assert LifecycleEvent.objects.get(pk=original.pk).stage == "started"


def test_existing_instance_and_queryset_update_paths_are_rejected():
    original = event()
    original.save()
    original.stage = "finished"

    with pytest.raises(TypeError, match="immutable"):
        original.save()
    with pytest.raises(TypeError, match="immutable"):
        original.save(force_insert=True)
    with pytest.raises(TypeError, match="immutable"):
        LifecycleEvent.objects.filter(pk=original.pk).update(stage="finished")
    with pytest.raises(TypeError, match="immutable"):
        LifecycleEvent.objects.bulk_update([original], ["stage"])
    with pytest.raises(TypeError, match="immutable"):
        LifecycleEvent.objects.update_or_create(
            pk=original.pk, defaults={"stage": "finished"}
        )

    assert LifecycleEvent.objects.get(pk=original.pk).stage == "started"


@pytest.mark.parametrize("positional", [False, True])
def test_bulk_upsert_cannot_overwrite_even_with_positional_options(positional):
    original = event()
    original.save()
    replacement = event(id=original.pk, key=original.key, stage="finished")

    args = ([replacement], None, False, True, ["stage"], ["key"])
    kwargs = {}
    if not positional:
        args = ([replacement],)
        kwargs = {
            "update_conflicts": True,
            "update_fields": ["stage"],
            "unique_fields": ["key"],
        }
    with pytest.raises(TypeError, match="immutable"):
        LifecycleEvent.objects.bulk_create(*args, **kwargs)

    assert LifecycleEvent.objects.get(pk=original.pk).stage == "started"


def test_insert_and_conflict_ignore_remain_available():
    first = event()
    first.save(False, False, None, None)
    second = event()
    LifecycleEvent.objects.bulk_create([second])
    duplicate = event(id=first.pk, key=first.key, stage="finished")
    LifecycleEvent.objects.bulk_create([duplicate], None, True)
    created, was_created = LifecycleEvent.objects.update_or_create(
        key=str(uuid4()), defaults={"stage": "started", "occurred_at": timezone.now()}
    )

    assert was_created
    assert created.stage == "started"
    assert LifecycleEvent.objects.count() == 3
    assert LifecycleEvent.objects.get(pk=first.pk).stage == "started"
