from django.contrib import admin

from ergonaut.utils.admin import register

from . import models


@register(models.PageActionCall)
class PageActionCallAdmin(admin.ModelAdmin):
    list_display = ["created_at", "bot", "action", "user", "page", "approved", "duration_ms", "outcome"]
    list_filter = ["bot", "action", "approved"]
    search_fields = ["action", "page", "user__username", "error"]
    date_hierarchy = "created_at"
    readonly_fields = [f.name for f in models.PageActionCall._meta.fields]

    @admin.display(description="Outcome")
    def outcome(self, obj):
        return f"failed: {obj.error}"[:100] if obj.error else "ok"

    def has_add_permission(self, request):
        return False
