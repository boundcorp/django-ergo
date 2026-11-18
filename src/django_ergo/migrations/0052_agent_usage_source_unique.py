from django.db import migrations
from django.db import models


class Migration(migrations.Migration):
    dependencies = [
        ("django_ergo", "0051_worker_originating_message"),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name="agentusage",
            name="agent_usage_worker_model",
        ),
        migrations.AddConstraint(
            model_name="agentusage",
            constraint=models.UniqueConstraint(
                fields=("worker", "model", "source"),
                name="agent_usage_worker_model_source",
            ),
        ),
    ]
