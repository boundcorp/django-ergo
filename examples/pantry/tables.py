from django.db import models

from django_ergo.bots import BotTable


class Pantry(BotTable):
    """What's in the pantry, and what's on order."""

    name = models.CharField(max_length=100)
    quantity = models.IntegerField(default=0)
    on_order = models.IntegerField(default=0)
