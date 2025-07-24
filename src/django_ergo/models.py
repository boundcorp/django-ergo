import uuid

from django.db import models

from django_ergo.mixins import TimeStampedMixin


class MyModel(TimeStampedMixin):
    """Example model."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    name = models.CharField(max_length=255)

    def __str__(self):
        return self.name
