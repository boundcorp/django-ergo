from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("django_ergo", "0046_cost_ledger")]

    operations = [
        migrations.AddField(
            model_name="inferencerequest",
            name="cost_ledger_state",
            field=models.CharField(db_index=True, default="", max_length=16),
        ),
    ]
