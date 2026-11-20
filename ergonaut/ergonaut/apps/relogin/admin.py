from django.contrib import admin

from ergonaut.apps.relogin.models import ReloginSession
from ergonaut.utils.admin import register


@register(ReloginSession)
class ReloginSessionAdmin(admin.ModelAdmin):
    """Read-only audit: what ran, for whom, and how it ended."""

    list_display = ["created_at", "bot_name", "store", "user", "outcome", "state", "exit_code", "account"]
    list_filter = ["bot_name", "store", "outcome"]
    readonly_fields = [field.name for field in ReloginSession._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
