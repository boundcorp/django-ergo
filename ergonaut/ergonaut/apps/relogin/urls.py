from django.urls import path

from ergonaut.apps.relogin import views

urlpatterns = [
    path("", views.start_page, name="relogin-start-page"),
    path("start/", views.start, name="relogin-start"),
    path("<str:session_id>/", views.session_page, name="relogin-session"),
    path("<str:session_id>/status", views.status, name="relogin-status"),
    path("<str:session_id>/input", views.submit, name="relogin-input"),
    path("<str:session_id>/cancel", views.cancel, name="relogin-cancel"),
]
