import json

from django.core.management.base import BaseCommand

from django_ergo.plugins.runpod.native import reconcile_once


class Command(BaseCommand):
    help = "Reconcile native RunPod rentals independently of bots, chats and Workers"

    def handle(self, *args, **options):
        self.stdout.write(json.dumps(reconcile_once(), sort_keys=True))
