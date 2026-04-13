from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("django_ergo", "0008_alter_conversationsession_transport_type"),
    ]

    operations = [
        migrations.AddField(
            model_name="knowledgebase",
            name="organization_strategy",
            field=models.TextField(
                blank=True,
                default="",
                help_text="Describes the hierarchy layout for this KB — what each tree prefix means, how articles should be organized.",
            ),
        ),
    ]
