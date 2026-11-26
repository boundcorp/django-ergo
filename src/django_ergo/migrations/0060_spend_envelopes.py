"""Spend envelopes: one stable spending identity (cumulative ledger and
advisory lock) per line of spend authority, with ``SpendGrant`` rows as its
immutable revisions (``bots.pools``).

Backfill, conservatively: every config-derived grant of the same pool and
owner joins one envelope (``config:<pool>:<owner id>``), so the spend and
holds of all its earlier revisions count against the current budget; an
issued grant is its own envelope. Nothing is un-revoked and no ledger row
changes (rows keep their ``grant:<id>`` key; totals are summed per envelope).

Reverse, conservatively: each envelope's ledger rows move to one of its
revisions (the current one, else the newest), because 0059 totals per grant
and would otherwise refund what earlier revisions spent or hold; superseded
revisions are revoked (0059 would treat them as current); and repeated
``config_key`` fingerprints are cleared on all but the newest row, so the
old unique constraint can come back.
"""

import uuid

import django.db.models.deletion
from django.conf import settings
from django.db import migrations
from django.db import models


def backfill(apps, schema_editor):
    SpendGrant = apps.get_model("django_ergo", "SpendGrant")
    SpendEnvelope = apps.get_model("django_ergo", "SpendEnvelope")
    for grant in SpendGrant.objects.order_by("created_at", "pk"):
        if grant.source == "config":
            key = f"config:{grant.pool}:{grant.owner_id}"
        else:
            key = f"issued:{grant.pk}"
        envelope, _ = SpendEnvelope.objects.get_or_create(
            key=key,
            defaults={
                "pool": grant.pool,
                "source": grant.source,
                "owner_id": grant.owner_id,
                "grantee_id": grant.grantee_id,
            },
        )
        SpendGrant.objects.filter(pk=grant.pk).update(envelope=envelope)


def unbackfill(apps, schema_editor):
    SpendGrant = apps.get_model("django_ergo", "SpendGrant")
    InferenceRequest = apps.get_model("django_ergo", "InferenceRequest")
    SpendEnvelope = apps.get_model("django_ergo", "SpendEnvelope")
    for envelope in SpendEnvelope.objects.all():
        revisions = list(
            SpendGrant.objects.filter(envelope=envelope).order_by("-created_at")
        )
        if len(revisions) <= 1:
            continue
        target = next(
            (g for g in revisions if g.revoked_at is None and g.superseded_at is None),
            revisions[0],
        )
        InferenceRequest.objects.filter(
            pool__in=[f"grant:{g.pk}"[:64] for g in revisions]
        ).update(pool=f"grant:{target.pk}"[:64])
    for grant in SpendGrant.objects.filter(
        superseded_at__isnull=False, revoked_at__isnull=True
    ):
        SpendGrant.objects.filter(pk=grant.pk).update(
            revoked_at=grant.superseded_at, revoked_reason="superseded"
        )
    seen = set()
    for grant in SpendGrant.objects.exclude(config_key="").order_by("-created_at"):
        if grant.config_key in seen:
            SpendGrant.objects.filter(pk=grant.pk).update(config_key="")
        seen.add(grant.config_key)


class Migration(migrations.Migration):
    dependencies = [
        ("django_ergo", "0059_model_pools"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="SpendEnvelope",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                ("key", models.CharField(max_length=160, unique=True)),
                ("pool", models.CharField(db_index=True, max_length=64)),
                ("source", models.CharField(default="issued", max_length=16)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "grantee",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "owner",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
        ),
        migrations.RemoveConstraint(
            model_name="spendgrant",
            name="spend_grant_config_key_unique",
        ),
        migrations.AddField(
            model_name="spendgrant",
            name="envelope",
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="revisions",
                to="django_ergo.spendenvelope",
            ),
        ),
        migrations.AddField(
            model_name="spendgrant",
            name="superseded_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.RunPython(backfill, unbackfill),
        migrations.AlterField(
            model_name="spendgrant",
            name="envelope",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="revisions",
                to="django_ergo.spendenvelope",
            ),
        ),
    ]
