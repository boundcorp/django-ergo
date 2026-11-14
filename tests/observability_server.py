"""A deterministic local HTTP server standing in for Grafana and Prometheus."""

from __future__ import annotations

import gzip
import json
import threading
import time
from http.server import BaseHTTPRequestHandler
from http.server import ThreadingHTTPServer
from urllib.parse import parse_qs
from urllib.parse import urlsplit


class Reply:
    def __init__(  # noqa: PLR0913
        self,
        status=200,
        body=b"",
        ctype="application/json",
        headers=None,
        delay=0,
        gzip_body=False,
    ):
        if not isinstance(body, bytes):
            body = json.dumps(body).encode()
        if gzip_body:
            body = gzip.compress(body)
            headers = {**(headers or {}), "Content-Encoding": "gzip"}
        self.status, self.body, self.ctype = status, body, ctype
        self.headers, self.delay = headers or {}, delay


class FakeServer:
    """``routes`` maps a path to a Reply (or a callable taking the request)."""

    def __init__(self):
        self.routes: dict[str, object] = {}
        self.requests: list[dict] = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                parts = urlsplit(self.path)
                request = {
                    "path": parts.path,
                    "query": {k: v[0] for k, v in parse_qs(parts.query).items()},
                    "headers": dict(self.headers),
                }
                owner.requests.append(request)
                route = owner.routes.get(parts.path)
                reply = route(request) if callable(route) else route
                if reply is None:
                    reply = Reply(404, {"message": "nope"})
                if reply.delay:
                    time.sleep(reply.delay)
                try:
                    self.send_response(reply.status)
                    self.send_header("Content-Type", reply.ctype)
                    self.send_header("Content-Length", str(len(reply.body)))
                    for key, value in reply.headers.items():
                        self.send_header(key, value)
                    self.end_headers()
                    self.wfile.write(reply.body)
                except OSError:
                    pass

            def do_POST(self):
                owner.requests.append({"path": self.path, "method": "POST"})
                self.send_response(405)
                self.end_headers()

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def success(data, **extra):
    return Reply(200, {"status": "success", "data": data, **extra})


def vector(*samples, ts=1_749_988_800):
    return {
        "resultType": "vector",
        "result": [
            {"metric": labels, "value": [ts, value]} for labels, value in samples
        ],
    }


def matrix(*series):
    return {
        "resultType": "matrix",
        "result": [
            {"metric": labels, "values": [[t, v] for t, v in values]}
            for labels, values in series
        ],
    }
