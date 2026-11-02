from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("django_ergo", "0040_worker_step_lease")]

    operations = [
        migrations.AddField(
            model_name="conversationsession",
            name="user_title",
            field=models.CharField(blank=True, max_length=200, null=True),
        ),
    ]
