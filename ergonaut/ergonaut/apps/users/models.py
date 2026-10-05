from django.contrib.auth.models import AbstractUser
from django.core import signing
from django.db import models

from ergonaut.utils.email import MailMixin
from ergonaut.utils.models import MediumIDMixin, TimestampMixin


class AccountTypes(models.TextChoices):
    STAFF = "staff", "Staff"
    USER = "user", "User"


class User(TimestampMixin, MediumIDMixin, AbstractUser, MailMixin):
    account_type = models.CharField(max_length=50, choices=AccountTypes.choices, default=AccountTypes.STAFF)
    # Read by Ergo bots: the current time in context and Telegram routing.
    timezone = models.CharField(max_length=64, blank=True, default="")
    telegram_id = models.BigIntegerField(null=True, blank=True, unique=True)

    def __str__(self):
        return f"{self.get_full_name()} <{self.email}>"

    def get_jwt_token(self, action, **extra_data):
        payload = {"username": self.username, "action": action}
        if extra_data:
            payload.update(**extra_data)
        token = signing.dumps(payload)
        return token

    def send_password_reset_email(self, invitation=False):
        token = self.get_jwt_token("password_reset")
        reset_url = f"auth/password-reset/{token}"
        title = "ergonaut"
        subject = f"{title}: {invitation and 'Account Invitation' or 'Password Reset'}"
        self.send_action_button_mail(
            subject,
            invitation and "Set Password" or "Reset Password",
            reset_url,
            invitation
            and [
                f"You've been invited to join '{self.name}' on {title}.",
                "Please click the button below to set your password and access your account.",
            ]
            or [
                f"You've requested a password reset for your {title} account.",
                "Please click the button below to reset your password.",
                "If you did not request a password reset, please ignore this email.",
            ],
        )


class ApiKey(TimestampMixin, MediumIDMixin):
    """A bearer token for the API, for scripts and agents (the Ergo client skill).

    Only a hash is stored: the key is shown once, when it's made. A key acts as
    its user, with the same access the user has in the web app.
    """

    PREFIX = "ergo_"

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="api_keys")
    name = models.CharField(max_length=100)
    hint = models.CharField(max_length=16)  # the first characters, to tell keys apart
    key_hash = models.CharField(max_length=64, unique=True)
    last_used_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ("-created_at",)

    def __str__(self):
        return f"{self.name} ({self.hint}…)"

    @staticmethod
    def hash(key: str) -> str:
        import hashlib

        return hashlib.sha256(key.encode()).hexdigest()

    @classmethod
    def issue(cls, user, name: str) -> tuple["ApiKey", str]:
        """A new key for ``user``: the row and the key itself (not stored anywhere)."""
        import secrets

        key = cls.PREFIX + secrets.token_urlsafe(32)
        row = cls.objects.create(user=user, name=name, hint=key[: len(cls.PREFIX) + 6], key_hash=cls.hash(key))
        return row, key

    @classmethod
    def user_for(cls, key: str):
        """The active user a key belongs to, or None. Notes when it was last used."""
        from django.utils import timezone

        if not key.startswith(cls.PREFIX):
            return None
        row = (
            cls.objects.select_related("user")
            .filter(key_hash=cls.hash(key), revoked_at__isnull=True, user__is_active=True)
            .first()
        )
        if row is None:
            return None
        now = timezone.now()
        if row.last_used_at is None or (now - row.last_used_at).total_seconds() > 60:
            cls.objects.filter(pk=row.pk).update(last_used_at=now)
        return row.user
