import uuid

from django.db import models


def _generate_medium_id():
    return "".join(str(uuid.uuid4()).split("-")[1:4])


class MediumIDMixin(models.Model):
    id = models.CharField(max_length=32, primary_key=True, default=_generate_medium_id, editable=False)

    class Meta:
        abstract = True


class TimestampMixin(models.Model):
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True, db_index=True)

    class Meta:
        abstract = True
        ordering = ("-updated_at", "-created_at")
