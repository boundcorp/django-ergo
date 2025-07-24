from django.contrib import admin
from django.urls import include  # noqa: F401
from django.urls import path

admin.site.enable_nav_sidebar = False

urlpatterns = [
    path("admin/", admin.site.urls),
    # Uncomment the next line to enable the admin and app urls
    # path("django-ergo/", include("django_ergo.urls", namespace="django-ergo")),
]
