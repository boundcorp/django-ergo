"""
Migration to create the pgvector extension before any models that depend on it.
This must run before 0001_initial.py.
"""
from django.db import migrations


class Migration(migrations.Migration):

    initial = True
    dependencies = []

    operations = [
        migrations.RunSQL(
            sql="CREATE EXTENSION IF NOT EXISTS vector;",
            reverse_sql="DROP EXTENSION IF EXISTS vector;",
        ),
    ]
