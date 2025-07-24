from django.contrib import admin

from .models import MyModel


@admin.register(MyModel)
class EventAdmin(admin.ModelAdmin):
    list_display = ["name", "created_at"]
    search_fields = ["name"]
