from django.urls import path

from .views import MyView

app_name = "django-ergo"

urlpatterns = [
    path("", MyView.as_view(), name="my-view"),
]
