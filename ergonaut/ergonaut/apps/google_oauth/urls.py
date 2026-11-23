from django.urls import path

from ergonaut.apps.google_oauth import views

urlpatterns = [
    path("google/", views.page, name="google-oauth-page"),
    path("google/start/", views.start, name="google-oauth-start"),
    path("google/callback/", views.callback, name="google-oauth-callback"),
    path("google/disconnect/", views.disconnect, name="google-oauth-disconnect"),
]
