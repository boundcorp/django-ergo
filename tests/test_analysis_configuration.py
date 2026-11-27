"""Offline settings, TLS trust and disabled cleanup regression coverage."""

import ssl
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from http.server import BaseHTTPRequestHandler
from http.server import ThreadingHTTPServer
from threading import Thread

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from django.core.exceptions import ImproperlyConfigured
from django.core.management import call_command

from django_ergo.analysis.client import ControllerClient
from django_ergo.analysis.client import configured_client
from django_ergo.analysis.config import controller_key
from django_ergo.analysis.config import enabled_value
from django_ergo.analysis.config import retention_days
from django_ergo.analysis.protocol import open_envelope
from django_ergo.analysis.protocol import seal
from django_ergo.analysis.runner import RunnerUnavailableError

KEY = b"fixture-analysis-key-32-bytes-long!!"


@pytest.mark.parametrize(
    "value", ["false", "FALSE", "0", "no", "off", "", " false ", False]
)
def test_explicit_off(value):
    assert enabled_value(value) is False


@pytest.mark.parametrize("value", ["true", "TRUE", "1", "yes", "on", " true ", True])
def test_explicit_on(value):
    assert enabled_value(value) is True


@pytest.mark.parametrize("value", ["enabled", "fixture-sensitive", None, 1])
def test_invalid_boolean_is_sanitized(value):
    with pytest.raises(
        ImproperlyConfigured, match="ERGO_ANALYSIS_ENABLED must be a boolean"
    ) as error:
        enabled_value(value)
    assert "fixture-sensitive" not in str(error.value)


@pytest.mark.parametrize(
    "value", [0, 366, -1, True, 1.5, "1.5", "-1", "", "9" * 5000, "fixture-sensitive"]
)
def test_invalid_retention_is_sanitized(value):
    with pytest.raises(ImproperlyConfigured, match="from 1 to 365") as error:
        retention_days(value)
    assert "fixture-sensitive" not in str(error.value)


@pytest.mark.parametrize("value", [1, "30", 365])
def test_retention_bounds(value):
    assert retention_days(value) == int(value)


def test_key_encoding_and_sanitized_failure():
    assert controller_key(KEY) is KEY
    assert controller_key("fixture-é") == "fixture-é".encode()
    with pytest.raises(ImproperlyConfigured, match="UTF-8") as error:
        controller_key("fixture-sensitive\udcff")
    assert "fixture-sensitive" not in str(error.value)


@pytest.mark.parametrize("enabled", [False, "false", "true", None])
def test_disabled_command_and_client_do_no_work(settings, monkeypatch, enabled):
    settings.ERGO_ANALYSIS_ENABLED = enabled

    def forbidden(*args, **kwargs):
        pytest.fail("Disabled feature allocated or accessed the controller")

    monkeypatch.setattr("django_ergo.analysis.client.ControllerClient", forbidden)
    monkeypatch.setattr(
        "django_ergo.management.commands.cleanup_analysis.configured_client", forbidden
    )
    call_command("cleanup_analysis")  # No DB fixture: any query also fails.
    with pytest.raises(RunnerUnavailableError, match="disabled"):
        configured_client()


@pytest.mark.parametrize("days", [0, 366, True, "fixture-sensitive"])
def test_cleanup_invalid_retention_precedes_client(settings, monkeypatch, days):
    settings.ERGO_ANALYSIS_ENABLED = True
    settings.ERGO_ANALYSIS_RETENTION_DAYS = days
    monkeypatch.setattr(
        "django_ergo.management.commands.cleanup_analysis.configured_client",
        lambda: pytest.fail("Invalid settings contacted the controller"),
    )
    with pytest.raises(ImproperlyConfigured, match="from 1 to 365"):
        call_command("cleanup_analysis")


def test_configured_client_encodes_key_and_passes_ca(settings, monkeypatch):
    settings.ERGO_ANALYSIS_ENABLED = True
    settings.ERGO_ANALYSIS_CONTROLLER_URL = "https://controller.invalid"
    settings.ERGO_ANALYSIS_CONTROLLER_KEY = KEY.decode()
    settings.ERGO_ANALYSIS_CONTROLLER_CA_FILE = "/fixture/ca.pem"
    monkeypatch.setattr(
        "django_ergo.analysis.client.ControllerClient",
        lambda *args, **kwargs: (args, kwargs),
    )
    assert configured_client() == (
        ("https://controller.invalid", KEY),
        {"ca_file": "/fixture/ca.pem"},
    )


@pytest.fixture
def tls_endpoint(tmp_path):
    """Ephemeral local CA/server, never host trust or a real key."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "fixture CA")])
    now = datetime.now(UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName("localhost")]), critical=False
        )
        .sign(key, hashes.SHA256())
    )
    ca = tmp_path / "fixture-ca.pem"
    ca.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    private = tmp_path / "fixture-key.pem"
    private.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            requests.append(self.path)
            raw = self.rfile.read(int(self.headers["Content-Length"]))
            nonce = open_envelope(KEY, raw)["nonce"]
            self.send_response(302 if self.path == "/redirect/v1" else 200)
            self.send_header("Location", "/target")
            self.end_headers()
            self.wfile.write(
                seal(KEY, {"request_nonce": nonce, "response": {"state": "ready"}})
            )

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(ca, private)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"https://localhost:{server.server_port}", ca, requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_private_ca_verified_without_ambient_proxies(tls_endpoint, monkeypatch):
    url, ca, requests = tls_endpoint
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("SSL_CERT_FILE", "/fixture/missing.pem")
    client = ControllerClient(url, KEY, ca_file=str(ca))
    assert client.verify.check_hostname is True
    assert client.verify.verify_mode == ssl.CERT_REQUIRED
    assert client.call({"action": "fixture"}) == {"state": "ready"}
    assert requests == ["/v1"]


@pytest.mark.parametrize("mismatch", ["untrusted", "hostname", "redirect"])
def test_tls_and_redirect_fail_closed(tls_endpoint, mismatch):
    url, ca, requests = tls_endpoint
    if mismatch == "hostname":
        url = url.replace("localhost", "127.0.0.1")
    if mismatch == "redirect":
        url += "/redirect"
    client = ControllerClient(
        url, KEY, ca_file=None if mismatch == "untrusted" else str(ca)
    )
    with pytest.raises(RunnerUnavailableError) as error:
        client.call({"action": "fixture"})
    assert KEY.decode() not in str(error.value)
    assert requests == (["/redirect/v1"] if mismatch == "redirect" else [])


@pytest.mark.parametrize("kind", ["missing", "invalid", "unreadable"])
def test_invalid_ca_is_actionable_and_sanitized(tmp_path, monkeypatch, kind):
    ca = tmp_path / "fixture-sensitive.pem"
    if kind == "invalid":
        ca.write_text("fixture-sensitive")
    if kind == "unreadable":

        def denied(*args, **kwargs):
            message = "fixture-sensitive"
            raise PermissionError(message)

        monkeypatch.setattr(ssl, "create_default_context", denied)
    with pytest.raises(
        RunnerUnavailableError, match="mount a readable PEM CA bundle"
    ) as error:
        ControllerClient("https://controller.invalid", KEY, ca_file=str(ca))
    assert "fixture-sensitive" not in str(error.value)
    assert error.value.__suppress_context__ is True


@pytest.mark.parametrize(
    "url",
    [
        "http://controller.invalid",
        "https://user:fixture-sensitive@controller.invalid",
        "https://controller.invalid?fixture-sensitive",
    ],
)
def test_https_endpoint_validation_is_sanitized(url):
    with pytest.raises(RunnerUnavailableError, match="HTTPS") as error:
        ControllerClient(url, KEY)
    assert "fixture-sensitive" not in str(error.value)


def test_network_exception_does_not_leak_key(monkeypatch):
    import httpx

    def failed_client(**kwargs):
        raise httpx.ConnectError(KEY.decode())

    monkeypatch.setattr(httpx, "Client", failed_client)
    with pytest.raises(RunnerUnavailableError, match="no execution fallback") as error:
        ControllerClient("https://controller.invalid", KEY).call({"action": "fixture"})
    assert KEY.decode() not in str(error.value)
    assert error.value.__suppress_context__ is True
