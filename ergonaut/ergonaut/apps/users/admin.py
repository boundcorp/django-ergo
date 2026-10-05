# type: ignore
from django.contrib import admin
from django.contrib.auth.admin import UserAdmin

from ergonaut.utils.admin import register

from . import models


@register(models.User)
class CustomUserAdmin(UserAdmin):
    list_display = [
        "email",
        "is_superuser",
        "is_staff",
        "date_joined",
        "last_login",
    ]
    search_fields = ["username", "email"]
    list_filter = [
        "is_staff",
    ]

    fieldsets = UserAdmin.fieldsets[0:2] + (
        (
            "Access",
            {
                "fields": [
                    "account_type",
                ]
            },
        ),
        ("Django Permissions", {"fields": ("is_active", "is_staff", "is_superuser")}),
    )

    add_fieldsets = (
        (
            None,
            {
                "classes": ("wide",),
                "fields": ("email", "password1", "password2"),
            },
        ),
        ("Personal Info", {"fields": ("first_name", "last_name")}),
        (
            "Access",
            {
                "fields": [
                    "account_type",
                ]
            },
        ),
    )


@register(models.ApiKey)
class ApiKeyAdmin(admin.ModelAdmin):
    list_display = ["name", "hint", "user", "created_at", "last_used_at", "revoked_at"]
    search_fields = ["name", "user__email", "user__username"]
    readonly_fields = ["hint", "key_hash", "last_used_at"]
