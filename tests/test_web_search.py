from __future__ import annotations

import io
import json
import subprocess
import threading
from http.server import BaseHTTPRequestHandler
from http.server import HTTPServer

import pytest
from django.contrib.auth import get_user_model

from django_ergo.plugins import web_search
from tests.test_bot_plugins import _turn_context
from tests.test_bots import make_bot
from tests.test_bots import say
from tests.test_conversation_structured import claude_tool

User = get_user_model()


def search_bot(tmp_path, *responses, config="backend: brave"):
    yaml_text = f"""
        name: researcher
        chats: {{main: {{skills: [web_search]}}}}
        plugins: [{{name: web_search, {config}}}]
    """
    bot, engine = make_bot(tmp_path, *responses, yaml_text=yaml_text, name="researcher")
    return bot, engine, bot.plugin("web_search")


def test_html_to_text_drops_markup_scripts_and_styles():
    title, text = web_search.html_to_text(
        "<html><head><title>Hi &amp; bye</title><style>p{}</style></head>"
        "<body><script>x()</script><h1>Head</h1><p>One  <b>two</b></p><li>three</li></body></html>"
    )
    assert title == "Hi & bye"
    assert text == "Head\nOne two\nthree"


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:9222/json",
        "http://localhost/",
        "http://169.254.169.254/latest/meta-data/",
        "http://10.0.0.5/",
        "http://[::1]/",
        "file:///etc/passwd",
    ],
)
def test_public_url_refuses_local_and_private_addresses(url):
    assert not web_search.public_url(url)


@pytest.mark.django_db
def test_fetch_refuses_local_addresses(tmp_path):
    _, _, plugin = search_bot(tmp_path)
    assert plugin.fetch("http://127.0.0.1:9222/json").startswith("Fetch refused")
    assert plugin.fetch("ftp://example.com/").startswith("Give an http")


@pytest.mark.django_db
def test_fetch_reads_a_page_as_text(tmp_path, monkeypatch):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = b"<title>Soup</title><p>Simmer for <i>20</i> minutes.</p>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setattr(web_search, "public_url", lambda url: True)
    try:
        _, _, plugin = search_bot(tmp_path)
        page = plugin.fetch(f"http://127.0.0.1:{server.server_port}/soup")
    finally:
        server.shutdown()
    assert page.startswith("# Soup\nhttp://127.0.0.1:")
    assert page.endswith("Simmer for 20 minutes.")


@pytest.mark.django_db
def test_brave_backend_formats_results(tmp_path, monkeypatch):
    monkeypatch.setenv("BRAVE_API_KEY", "k")
    seen = {}

    def urlopen(request, timeout):
        seen["url"] = request.full_url
        seen["token"] = request.get_header("X-subscription-token")
        data = {
            "web": {
                "results": [
                    {
                        "title": "Django",
                        "url": "https://djangoproject.com",
                        "description": "The <strong>web</strong> framework",
                        "age": "2 days ago",
                    }
                ]
            }
        }
        return io.BytesIO(json.dumps(data).encode())

    monkeypatch.setattr(web_search.urllib.request, "urlopen", urlopen)
    _, _, plugin = search_bot(tmp_path)
    result = plugin.search("django orm", count=3)
    assert result == (
        "- [Django](https://djangoproject.com) (2 days ago): The web framework"
    )
    assert "q=django+orm" in seen["url"]
    assert "count=3" in seen["url"]
    assert seen["token"] == "k"


@pytest.mark.django_db
def test_claude_code_backend_runs_cli_with_only_websearch(tmp_path, monkeypatch):
    calls = []

    def run(args, **kwargs):
        calls.append((args, kwargs))
        out = json.dumps({"is_error": False, "result": "- [A](https://a.example): a"})
        return subprocess.CompletedProcess(args, 0, stdout=out, stderr="")

    monkeypatch.setattr(web_search.subprocess, "run", run)
    _, _, plugin = search_bot(tmp_path, config="backend: claude_code")
    assert plugin.search("ergo") == "- [A](https://a.example): a"
    args, kwargs = calls[0]
    assert args[args.index("--tools") + 1] == "WebSearch"
    assert args[args.index("--allowedTools") + 1] == "WebSearch"
    assert args[args.index("--model") + 1] == "haiku"
    assert "Search the web for: ergo" in args[args.index("-p") + 1]
    assert not any(key.startswith("ANTHROPIC_") for key in kwargs["env"])

    def fail(args, **kwargs):
        return subprocess.CompletedProcess(args, 1, stdout="", stderr="Not logged in")

    monkeypatch.setattr(web_search.subprocess, "run", fail)
    assert plugin.search("ergo") == "Search failed: claude CLI exited 1: Not logged in"


@pytest.mark.django_db
def test_auto_backend_prefers_brave_then_claude_code_then_anthropic(
    tmp_path, monkeypatch
):
    from django_ergo.conversation.engines import claude_code

    _, _, plugin = search_bot(tmp_path, config="backend: auto")
    monkeypatch.setenv("BRAVE_API_KEY", "k")
    assert plugin.resolved_backend() == "brave"
    monkeypatch.delenv("BRAVE_API_KEY")
    monkeypatch.setattr(claude_code, "claude_installed", lambda command="": True)
    assert plugin.resolved_backend() == "claude_code"
    monkeypatch.setattr(claude_code, "claude_installed", lambda command="": False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    assert plugin.resolved_backend() == "anthropic"
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    assert plugin.search("x").startswith("Search failed: No web search backend")


def test_unknown_backend_is_an_error(tmp_path):
    with pytest.raises(ValueError, match="backend must be one of"):
        search_bot(tmp_path, config="backend: google")


@pytest.mark.django_db(transaction=True)
async def test_bot_searches_in_a_turn(tmp_path, monkeypatch):
    bot, engine, plugin = search_bot(
        tmp_path,
        claude_tool("ergo_web_search", {"query": "ergo release"}, tool_id="s1"),
        say("It shipped."),
    )
    monkeypatch.setattr(
        plugin,
        "_search_brave",
        lambda query, count: f"- [{query}](https://x.example): ok",
    )
    user = await User.objects.acreate(username="search-user")
    root = await bot.root_session(user)
    result = await bot.ask(root, "Did ergo ship?")
    assert result.text == "It shipped."
    assert "ergo_web_search searches the web" in _turn_context(engine._client.calls[0])
    second = json.dumps(engine._client.calls[1]["messages"])
    assert "https://x.example" in second
