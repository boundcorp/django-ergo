from django.contrib import admin
from django.urls import include  # noqa: F401
from django.urls import path
from .api import api

admin.site.enable_nav_sidebar = False

urlpatterns = [
    path("admin/", admin.site.urls),
    # Django Ergo core plugin
    path("django-ergo/", include("django_ergo.urls", namespace="django-ergo")),
    
    # API Example (demonstrates how to build APIs with django-ergo)
    path("api/", api.urls),
]
