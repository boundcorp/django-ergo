from django.db import migrations
from django.db import models


class Migration(migrations.Migration):
    dependencies = [("django_ergo", "0009_knowledgebase_organization_strategy")]
    operations = [
        migrations.AddField(
            model_name="article",
            name="status",
            field=models.CharField(
                choices=[
                    ("active", "Active"),
                    ("draft", "Draft"),
                    ("stale", "Stale"),
                    ("archived", "Archived"),
                    ("superseded", "Superseded"),
                ],
                default="active",
                max_length=16,
            ),
        ),
    ]
