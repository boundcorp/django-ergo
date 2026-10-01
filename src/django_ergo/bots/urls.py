from django.urls import path

from django_ergo.bots.webhooks import webhook_view

app_name = "ergo_bots"

urlpatterns = [
    path("<str:bot>/<str:plugin>/<str:hook>/", webhook_view, name="webhook"),
]
