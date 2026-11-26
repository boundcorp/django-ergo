"""Spend envelope families and stable config identity (``bots.pools``).

- ``parent``: an issued grant's envelope is drawn from its owner's config
  envelope on the pool. Every send on it is intersected with the parent's
  current authority, and the parent's budget caps the cumulative spend of
  the parent and all its children.
- ``accounts``: the metered accounts a config envelope has funded, so a
  renamed (or removed and re-added) pool continues its envelope instead of
  being funded again by config sync.
- ``linked_by``/``linked_at``/``link_note``: an admin's explicit link (or
  approval of new accounts); ``restored_at``: an admin's explicit lift of a
  person's revocation.
- ``ProviderToggle.accounts``: a switched-off provider's account identity, so
  renaming the provider in providers.yaml doesn't switch spend back on.

Backfill, conservatively: an issued envelope's parent is its owner's config
envelope on the same pool, when there is one (an issued envelope without a
parent authorizes nothing after this change). A config envelope's accounts
are the providers of every route any of its revisions held (the next config
sync adds key env and account names). Nothing is revoked or un-revoked and
no ledger row changes.

Reverse drops the columns; the code of 0060 then treats issued grants as
separately funded again (as it did before this migration).
"""

import django.db.models.deletion
from django.conf import settings
from django.db import migrations
from django.db import models


def backfill(apps, schema_editor):
    SpendEnvelope = apps.get_model("django_ergo", "SpendEnvelope")
    SpendGrant = apps.get_model("django_ergo", "SpendGrant")
    configs = {e.key: e for e in SpendEnvelope.objects.filter(source="config")}
    for envelope in configs.values():
        accounts = set()
        for routes in SpendGrant.objects.filter(envelope=envelope).values_list(
            "routes", flat=True
        ):
            for ref in routes or []:
                head = str(ref).partition("/")[0]
                if head:
                    accounts.add(f"provider:{head}")
        envelope.accounts = sorted(accounts)
        envelope.save(update_fields=["accounts"])
    for envelope in SpendEnvelope.objects.exclude(source="config"):
        parent = configs.get(f"config:{envelope.pool}:{envelope.owner_id}")
        if parent is not None:
            envelope.parent = parent
            envelope.save(update_fields=["parent"])


class Migration(migrations.Migration):
    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("django_ergo", "0060_spend_envelopes"),
    ]

    operations = [
        migrations.AddField(
            model_name="spendenvelope",
            name="parent",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="children",
                to="django_ergo.spendenvelope",
            ),
        ),
        migrations.AddField(
            model_name="spendenvelope",
            name="accounts",
            field=models.JSONField(blank=True, default=list),
        ),
        migrations.AddField(
            model_name="spendenvelope",
            name="linked_by",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="+",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddField(
            model_name="spendenvelope",
            name="linked_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="spendenvelope",
            name="link_note",
            field=models.CharField(blank=True, default="", max_length=200),
        ),
        migrations.AddField(
            model_name="spendenvelope",
            name="restored_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="providertoggle",
            name="accounts",
            field=models.JSONField(blank=True, default=list),
        ),
        migrations.RunPython(backfill, migrations.RunPython.noop),
    ]
