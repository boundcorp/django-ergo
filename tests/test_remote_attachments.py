"""Binary uploads use the attachment API before sending a chat message."""

import importlib.util
import io
import json
import threading
from email.parser import BytesParser
from email.policy import default
from http.server import BaseHTTPRequestHandler
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

from PIL import Image


def test_remote_send_uploads_file_bytes_then_attaches_id(tmp_path):
    path = (
        Path(__file__).parents[1]
        / "src/django_ergo/bots/skill_library/ergo-client/scripts/ergonaut_remote.py"
    )
    spec = importlib.util.spec_from_file_location("remote_for_upload", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    output = io.BytesIO()
    Image.new("RGB", (2, 2), "green").save(output, format="PNG")
    binary = output.getvalue()
    image = tmp_path / "preview.png"
    image.write_bytes(binary)
    svg_data = (
        b'<svg xmlns="http://www.w3.org/2000/svg"><rect width="2" height="2"/></svg>'
    )
    svg = tmp_path / "artwork.svg"
    svg.write_bytes(svg_data)
    received = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            received.append((self.path, self.headers["Content-Type"], body))
            result = (
                {"id": f"attachment-{len(received)}"}
                if self.path.endswith("/attachments")
                else {"queued": True}
            )
            output = json.dumps(result).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(output)))
            self.end_headers()
            self.wfile.write(output)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        client = module.Client(f"http://127.0.0.1:{server.server_port}", "test")
        args = SimpleNamespace(
            wait=False, interrupt=False, json=False, attach=[str(image), str(svg)]
        )
        assert module.send_and_maybe_wait(client, "session-1", "See artwork", args) == 0
    finally:
        server.shutdown()
        thread.join()
        server.server_close()
    assert [item[0] for item in received] == [
        "/api/sessions/session-1/attachments",
        "/api/sessions/session-1/attachments",
        "/api/sessions/session-1/messages",
    ]
    for (_, mime, body), name, data in zip(
        received[:2], ("preview.png", "artwork.svg"), (binary, svg_data), strict=True
    ):
        multipart = BytesParser(policy=default).parsebytes(
            f"Content-Type: {mime}\r\nMIME-Version: 1.0\r\n\r\n".encode() + body
        )
        [part] = list(multipart.iter_parts())
        assert part.get_filename() == name
        assert part.get_payload(decode=True) == data
    assert json.loads(received[2][2]) == {
        "text": "See artwork",
        "mode": "send",
        "attachment_ids": ["attachment-1", "attachment-2"],
    }
    assert image.read_bytes() == binary
    assert svg.read_bytes() == svg_data
