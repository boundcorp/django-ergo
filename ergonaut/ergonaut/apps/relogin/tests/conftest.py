import sys
import textwrap
import types

import pytest

from ergonaut.apps.relogin import stores
from ergonaut.apps.users.factories import UserFactory

STATE = "st4te-Abc_123"
AUTH_URL = (
    "https://claude.ai/oauth/authorize?code=true&client_id=9d1c250a&response_type=code"
    "&redirect_uri=http%3A%2F%2Flocalhost%3A54545%2Fcallback&scope=user%3Aprofile"
    f"&code_challenge=Zx9&code_challenge_method=S256&state={STATE}"
)
DEVICE_URL = "https://auth.openai.com/codex/device"
DEVICE_CODE = "WXYZ-12345"


class FakeRedis:
    def __init__(self):
        self.data = {}
        self.ttl = {}

    def set(self, key, value, ex=None, nx=False):
        if nx and key in self.data:
            return None
        self.data[key] = value.encode() if isinstance(value, str) else value
        self.ttl[key] = ex
        return True

    def get(self, key):
        return self.data.get(key)

    def getdel(self, key):
        return self.data.pop(key, None)

    def exists(self, key):
        return int(key in self.data)

    def delete(self, *keys):
        for key in keys:
            self.data.pop(key, None)


@pytest.fixture
def redis(monkeypatch, settings):
    fake = FakeRedis()
    monkeypatch.setattr("ergonaut.apps.relogin.handoff.client", lambda: fake)
    settings.CELERY_BROKER_URL = "redis://fake"
    return fake


STORES = {
    "omp/anthropic": {
        "label": "Anthropic login in omp",
        "command": "omp login anthropic",
        "shape": "paste_redirect",
        "url_hosts": ["claude.ai"],
        "port": 54545,
        "max_seconds": 300,
    },
    "claude-cli": {
        "label": "Claude CLI login",
        "command": "claude auth login --claudeai",
        "shape": "paste_code",
        "url_hosts": ["claude.ai", "claude.com"],
    },
    "codex-cli": {
        "label": "Codex CLI login",
        "command": "codex login --device-auth",
        "shape": "device_code",
        "url_hosts": ["auth.openai.com"],
    },
    "evil": {"label": "x", "command": "bash -c id", "shape": "device_code", "url_hosts": ["claude.ai"]},
}


class FakeBot:
    name = "devbox"

    def __init__(self, module):
        self.tool_modules = [types.SimpleNamespace(module=module, path=None)]

    async def chat_session(self, user, name):
        raise RuntimeError("no chats in tests")


@pytest.fixture
def bot(monkeypatch, tmp_path):
    host_key = tmp_path / "host.pub"
    host_key.write_text("ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFakeKeyForTests devbox\n")
    calls = {"identity": [], "post_check": []}
    module = types.SimpleNamespace(
        RELOGIN_STORES=STORES,
        RELOGIN_SSH={"host": "devbox-ssh.test", "port": 2222, "user": "dev", "host_key": str(host_key)},
        relogin_identity=lambda ctx, key: calls["identity"].append(key) or "hash-before",
        relogin_post_check=lambda ctx, key, before: calls["post_check"].append((key, before))
        or {"ok": True, "state": "ok", "detail": "Refresh grant ends 2026-12-19", "account": "same"},
    )
    fake = FakeBot(module)
    fake.calls = calls
    fake.module = module
    monkeypatch.setattr(
        stores,
        "get_bot",
        lambda name: fake if name == "devbox" else (_ for _ in ()).throw(stores.StoreError("No such bot.")),
    )
    return fake


@pytest.fixture
def staff(db, client):
    user = UserFactory(is_staff=True)
    client.force_login(user)
    return user


def fake_cli(tmp_path, body: str) -> list[str]:
    script = tmp_path / "fake_cli.py"
    script.write_text(textwrap.dedent(body))
    return [sys.executable, "-u", str(script)]


# Terminal output of the commands as their sources print it (omp 18.12, Claude Code 2.1.329,
# codex 0.164), with fake values.
OMP_ANTHROPIC = f"""
import sys, time
print("")
print("Open this URL in your browser:")
print("{AUTH_URL}")
print("Local shortcut (this machine only): http://localhost:54545/launch")
print("Complete login in your browser. If the browser cannot reach this machine, paste the final redirect URL or authorization code when prompted.")
print("")
print("Waiting for browser authentication...")
sys.stdout.write("Paste the authorization code (or full redirect URL): ")
sys.stdout.flush()
time.sleep(60)
"""

CLAUDE_URL = (
    "https://claude.com/cai/oauth/authorize?code=true&client_id=9d1c250a&response_type=code"
    "&redirect_uri=https%3A%2F%2Fplatform.claude.com%2Foauth%2Fcode%2Fcallback&scope=user%3Ainference"
    f"&code_challenge=Zx9&code_challenge_method=S256&state={STATE}"
)
CLAUDE_PASTE = f"""
import sys
print("Opening browser to sign in…")
print("If the browser didn't open, visit: \\x1b]8;;{CLAUDE_URL}\\x07\\x1b[94m{CLAUDE_URL}\\x1b[39m\\x1b]8;;\\x07")
sys.stdout.write("Paste code here if prompted > ")
sys.stdout.flush()
line = sys.stdin.readline().strip()
if line == "abcDEF123_xyz#{STATE}":
    print("Signing in…")
    print("Login successful.")
    sys.exit(0)
sys.stderr.write("Couldn't log in: invalid code\\n")
sys.exit(1)
"""

CODEX_DEVICE = f"""
import sys, time
print("")
print("Welcome to Codex [v\\x1b[90m0.164.0\\x1b[0m]")
print("\\x1b[90mOpenAI's command-line coding agent\\x1b[0m")
print("")
print("Follow these steps to sign in with ChatGPT using device code authorization:")
print("")
print("1. Open this link in your browser and sign in to your account")
print("   \\x1b[94m{DEVICE_URL}\\x1b[0m")
print("")
print("2. Enter this one-time code \\x1b[90m(expires in 15 minutes)\\x1b[0m")
print("   \\x1b[94m{DEVICE_CODE}\\x1b[0m")
print("")
print("\\x1b[90mContinue only if you started this login in Codex. If a website or another person gave you this code, cancel.\\x1b[0m")
sys.stdout.flush()
time.sleep(1.5)
print("Successfully logged in")
"""

OMP_DEVICE = f"""
import sys, time
print("Initiating device authorization…")
print("")
print("Open this URL in your browser:")
print("{DEVICE_URL}")
print("Enter code: {DEVICE_CODE}")
print("")
print("Waiting for browser authorization (code: {DEVICE_CODE})…")
sys.stdout.flush()
time.sleep(1.5)
print("\\x1b[32m\\nLogged in to ChatGPT Plus/Pro (Codex, headless/device) as a@b.c\\x1b[39m")
"""
