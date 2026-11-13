"""Regression coverage for the observability HTTP security boundary."""

import re
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler
from http.server import ThreadingHTTPServer

import pytest

from django_ergo.observability.errors import ObservabilityError
from django_ergo.observability.http import BoundedClient
from django_ergo.observability.http import Limits


@contextmanager
def fixture_server(handler):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def client(url, token=None):
    return BoundedClient(
        url,
        allowed=[re.compile(r"/test")],
        token=token,
        limits=Limits(connect_timeout=0.1, read_timeout=0.1, total_timeout=0.15),
    )


@pytest.mark.parametrize(
    ("drip_headers", "chunked"),
    [(True, False), (False, False), (False, True)],
    ids=["headers", "body", "chunked-body"],
)
def test_dripping_response_is_stopped_at_wall_clock_deadline(drip_headers, chunked):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            header = (
                b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n"
                if chunked
                else b"HTTP/1.1 200 OK\r\nContent-Length: 30\r\n\r\n"
            )
            try:
                if drip_headers:
                    for byte in header:
                        self.wfile.write(bytes([byte]))
                        self.wfile.flush()
                        time.sleep(0.025)
                    self.wfile.write(b"x" * 30)
                else:
                    self.wfile.write(header)
                    self.wfile.flush()
                    for _ in range(30):
                        self.wfile.write(b"1\r\nx\r\n" if chunked else b"x")
                        self.wfile.flush()
                        time.sleep(0.025)
            except OSError:
                pass

    with fixture_server(Handler) as url:
        start = time.monotonic()
        with pytest.raises(ObservabilityError) as exc:
            client(url).get("/test")
        elapsed = time.monotonic() - start
        assert exc.value.code == "timeout"
        assert elapsed < 0.5


@pytest.mark.parametrize(
    "response",
    [
        b"HTTP/1.1 200 OK\r\nContent-Length: 3\r\n\r\nabc",
        b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n3\r\nabc\r\n0\r\n\r\n",
        b"HTTP/1.0 200 OK\r\n\r\nabc",
    ],
    ids=["content-length", "chunked", "connection-close"],
)
def test_fast_responses_remain_readable(response):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            self.wfile.write(response)
            self.wfile.flush()

    with fixture_server(Handler) as url:
        assert client(url).get("/test").body == b"abc"


@pytest.mark.parametrize("header", ["Content-Type", "Content-Encoding"])
def test_upstream_header_diagnostics_redact_credentials(header):
    token = "FAKE_REVIEW_TOKEN"

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            self.send_response(200)
            self.send_header(header, token)
            self.send_header("Content-Length", "3")
            self.end_headers()
            self.wfile.write(b"bad")

    with fixture_server(Handler) as url, pytest.raises(ObservabilityError) as exc:
        client(url, token).get_json("/test")
    assert token not in str(exc.value)
    assert "[redacted]" in str(exc.value)


def test_sanitizer_redacts_before_clipping_and_preserves_other_text():
    bounded = client("http://127.0.0.1:1", token="s e c r e t")
    assert bounded.sanitize(" s e c r e t   detail", limit=20) == " [redacted]   detail"
    assert bounded.sanitize("s e c r e t", limit=5) == "[reda"
