"""Run local Django checks with keyless parent and Python loopback-only sockets."""

# ruff: noqa: INP001
import sys

import pytest_socket

pytest_socket.socket_allow_hosts(
    ["127.0.0.1", "::1", "localhost"], allow_unix_socket=True
)

from django.core.management import execute_from_command_line  # noqa: E402

execute_from_command_line(
    ["manage.py", *sys.argv[1:], "--settings=ergonaut.settings.test_settings"]
)
