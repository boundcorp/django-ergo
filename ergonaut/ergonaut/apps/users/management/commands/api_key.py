from django.core.management.base import BaseCommand, CommandError
from django.db.models import Q
from django.utils import timezone

from ergonaut.apps.users.models import ApiKey, User


class Command(BaseCommand):
    help = "Make, list and revoke API keys (Authorization: Bearer ergo_...)."

    def add_arguments(self, parser):
        sub = parser.add_subparsers(dest="action", required=True)
        create = sub.add_parser("create", help="Make a key for a user and print it once")
        create.add_argument("user", help="Username or email")
        create.add_argument("--name", required=True, help="Where it's used, e.g. rigel-claude")
        listing = sub.add_parser("list", help="Active keys")
        listing.add_argument("user", nargs="?", help="Only this user's keys")
        revoke = sub.add_parser("revoke", help="Revoke a key by id")
        revoke.add_argument("id")

    def handle(self, *args, action, **options):
        if action == "create":
            user = User.objects.filter(Q(username=options["user"]) | Q(email=options["user"])).first()
            if user is None:
                raise CommandError(f"No user {options['user']!r}")
            row, key = ApiKey.issue(user, options["name"])
            self.stderr.write(
                f"Made key {row.id} ({row.name}) for {user.email or user.username}. It won't be shown again:"
            )
            self.stdout.write(key)
        elif action == "list":
            rows = ApiKey.objects.filter(revoked_at__isnull=True).select_related("user")
            if options.get("user"):
                rows = rows.filter(Q(user__username=options["user"]) | Q(user__email=options["user"]))
            for row in rows:
                used = row.last_used_at.isoformat(timespec="minutes") if row.last_used_at else "never"
                self.stdout.write(
                    f"{row.id}  {row.hint}…  {row.name}  {row.user.email or row.user.username}  used {used}"
                )
        elif action == "revoke":
            if not ApiKey.objects.filter(id=options["id"], revoked_at__isnull=True).update(revoked_at=timezone.now()):
                raise CommandError(f"No active key {options['id']!r}")
            self.stdout.write(f"Revoked {options['id']}")
