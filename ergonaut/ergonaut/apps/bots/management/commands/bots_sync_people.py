from django.core.management.base import BaseCommand

from ergonaut.apps.bots.loading import find_setup
from ergonaut.apps.bots.loading import sync_people


class Command(BaseCommand):
    help = "Create or update a Django user for each person in the bot config."

    def handle(self, *args, **options):
        setup = find_setup()
        created = sync_people(setup)
        self.stdout.write(f"{len(setup.people)} people, {len(created)} new: {', '.join(created) or '-'}")
