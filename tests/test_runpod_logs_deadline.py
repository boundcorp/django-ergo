"""A socket idle timeout alone cannot bound RunPod's streaming log response."""

import threading
import time
from http.server import BaseHTTPRequestHandler
from http.server import ThreadingHTTPServer
from urllib.request import urlopen

import pytest

from django_ergo.plugins.runpod.transport import ProviderError
from django_ergo.plugins.runpod.transport import RunpodREST
from tests.test_runpod_ssh import HTTP
from tests.test_runpod_ssh import OWNER
from tests.test_runpod_ssh import pod


def test_partial_sse_line_has_total_deadline():
    stopped = threading.Event()

    class Logs(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            try:
                # Each byte resets the socket idle timeout. No newline means
                # readline cannot return to the caller's deadline check.
                while not stopped.wait(0.01):
                    self.wfile.write(b":")
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Logs)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    class Provider(HTTP):
        def open(self, request, timeout):
            if "/logs?" in request.full_url:
                return urlopen(
                    f"http://127.0.0.1:{server.server_port}/", timeout=timeout
                )
            return super().open(request, timeout)

    try:
        provider = Provider(pod())
        client = RunpodREST(lambda: "fixture-secret", opener=provider, timeout=0.15)
        started = time.monotonic()
        with pytest.raises(ProviderError, match="authenticated SSH"):
            client.ssh_connection("pod123", OWNER, deadline=time.time() + 30)
        assert time.monotonic() - started < 1
        assert len(provider.requests) == 1  # no final address read after failed pin
    finally:
        stopped.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
