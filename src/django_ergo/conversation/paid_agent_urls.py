"""Include under /v1/ on the separately confined, credential-holding proxy only."""

from django.urls import re_path

from .paid_agent_proxy import proxy_view

urlpatterns = [
    re_path(r"^(?P<endpoint>responses|chat/completions|revoke)$", proxy_view)
]
