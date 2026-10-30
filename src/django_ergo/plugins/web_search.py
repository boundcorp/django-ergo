"""Web search plugin: search the web and read pages, for any engine.

    plugins:
      - name: web_search
        backend: auto              # auto, brave, claude_code or anthropic
        brave_api_key_env: BRAVE_API_KEY
        anthropic_api_key_env: ANTHROPIC_API_KEY
        model: ""                  # claude_code: haiku; anthropic: claude-haiku-5-5
        count: 8                   # results per search
        timeout: 90                # seconds per search or fetch
        max_page_chars: 20000      # ergo_web_fetch trims pages to this

``ergo_web_search(query)`` returns titles, URLs and snippets, and
``ergo_web_fetch(url)`` returns a page as text. The tools are the same on
every engine, so a chat keeps them when the router moves it to another
model. Where a search runs is the backend:

- ``brave``: the Brave Search API. Fast, structured results; needs a key.
- ``claude_code``: a short ``claude -p`` call with only Claude Code's
  WebSearch tool, on the subscription login this host already uses for the
  Claude Code engine (see ``engines.claude_code``). No extra cost on the
  subscription, but it takes a model call per search.
- ``anthropic``: one Messages API call with Anthropic's server-side web
  search tool, billed per search on the API key.
- ``auto`` picks ``brave`` when its key is set, else ``claude_code`` when
  the CLI is installed, else ``anthropic`` when its key is set.

Fetching is a plain HTTP GET from this host, to public addresses only (not
localhost or the private network, even through a redirect). Pages that need a sign-in, or
that block datacenter IPs, are the ``browser`` plugin's job.
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import socket
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from typing import TYPE_CHECKING

from django_ergo.bots.plugins import BotPlugin
from django_ergo.bots.tools import BotTool
from django_ergo.bots.tools import FunctionToolkit
from django_ergo.bots.tools import bot_tool
from django_ergo.conversation.context import TextContextSource
from django_ergo.plugins.bash import trim

if TYPE_CHECKING:
    from django_ergo.bots.tools import ToolContext
    from django_ergo.conversation.context import ContextSource
    from django_ergo.conversation.toolkit import Toolkit

BACKENDS = ("auto", "brave", "claude_code", "anthropic")
BRAVE_URL = "https://api.search.brave.com/res/v1/web/search"
USER_AGENT = (
    "Mozilla/5.0 (compatible; ErgoBot/1.0; +https://github.com/boundcorp/django-ergo)"
)
SEARCH_PROMPT = (
    "Search the web for: {query}\n\n"
    "Answer with up to {count} of the most relevant results, best first, one per "
    "line as `- [title](url): one or two sentences on what the page says about the "
    "query`. Then add a short paragraph with the answer if the results give one. "
    "Only list pages the search returned."
)


class WebSearchError(RuntimeError):
    pass


class _TextExtractor(HTMLParser):
    """Readable text from HTML: no scripts, styles or markup, one line per block."""

    SKIP = frozenset({"script", "style", "noscript", "svg", "template", "head"})
    BLOCK = frozenset(
        {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "section",
         "article", "header", "footer", "pre", "blockquote", "table", "ul", "ol"}
    )  # fmt: skip

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.title = ""
        self._skip = 0
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self._skip += 1
        if tag == "title":
            self._in_title = True
        if tag in self.BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self.SKIP and self._skip:
            self._skip -= 1
        if tag == "title":
            self._in_title = False
        if tag in self.BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        elif not self._skip:
            self.parts.append(data)

    def text(self) -> str:
        lines = (
            re.sub(r"\s+", " ", line).strip()
            for line in "".join(self.parts).split("\n")
        )
        return "\n".join(line for line in lines if line)


def public_url(url: str) -> bool:
    """Whether a URL is http(s) on a host whose every address is public. Keeps
    fetches off this host's own services (Chrome's debug port, cloud metadata)."""
    parts = urllib.parse.urlparse(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return False
    try:
        infos = socket.getaddrinfo(parts.hostname, parts.port or None)
    except (OSError, UnicodeError):
        return False
    return bool(infos) and all(
        ipaddress.ip_address(info[4][0].split("%")[0]).is_global for info in infos
    )


class _PublicRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: PLR0913
        if not public_url(newurl):
            reason = f"redirect to a non-public address: {newurl}"
            raise urllib.error.URLError(reason)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def html_to_text(html: str) -> tuple[str, str]:
    """(title, text) of an HTML page."""
    parser = _TextExtractor()
    parser.feed(html)
    parser.close()
    return parser.title.strip(), parser.text()


class WebSearchPlugin(BotPlugin):
    name = "web_search"
    description = "Search the web and read pages"

    def on_load(self) -> None:
        self.backend = str(self.config.get("backend") or "auto")
        if self.backend not in BACKENDS:
            msg = f"web_search backend must be one of {', '.join(BACKENDS)}"
            raise ValueError(msg)
        self.brave_key_env = str(
            self.config.get("brave_api_key_env") or "BRAVE_API_KEY"
        )
        self.anthropic_key_env = str(
            self.config.get("anthropic_api_key_env") or "ANTHROPIC_API_KEY"
        )
        self.model = str(self.config.get("model") or "")
        self.count = int(self.config.get("count", 8))
        self.timeout = int(self.config.get("timeout", 90))
        self.max_page_chars = int(self.config.get("max_page_chars", 20_000))

    # -- backends ----------------------------------------------------------

    def resolved_backend(self) -> str:
        if self.backend != "auto":
            return self.backend
        from django_ergo.conversation.engines.claude_code import claude_installed

        if os.environ.get(self.brave_key_env):
            return "brave"
        if claude_installed():
            return "claude_code"
        if os.environ.get(self.anthropic_key_env):
            return "anthropic"
        msg = (
            f"No web search backend: set {self.brave_key_env}, install and log in "
            f"the claude CLI, or set {self.anthropic_key_env}"
        )
        raise WebSearchError(msg)

    def search(self, query: str, count: int = 0) -> str:
        query = query.strip()
        if not query:
            return "Give a search query."
        count = max(1, min(int(count or self.count), 20))
        try:
            backend = self.resolved_backend()
            return getattr(self, f"_search_{backend}")(query, count)
        except WebSearchError as exc:
            return f"Search failed: {exc}"

    def _search_brave(self, query: str, count: int) -> str:
        key = os.environ.get(self.brave_key_env)
        if not key:
            msg = f"{self.brave_key_env} is not set"
            raise WebSearchError(msg)
        url = f"{BRAVE_URL}?{urllib.parse.urlencode({'q': query, 'count': count})}"
        request = urllib.request.Request(  # noqa: S310 — a fixed https URL
            url,
            headers={"Accept": "application/json", "X-Subscription-Token": key},
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:  # noqa: S310
                data = json.load(response)
        except (urllib.error.URLError, TimeoutError, ValueError) as exc:
            msg = f"Brave Search: {exc}"
            raise WebSearchError(msg) from exc
        results = ((data.get("web") or {}).get("results") or [])[:count]
        if not results:
            return f"No results for {query!r}."
        lines = []
        for result in results:
            snippet = html_to_text(result.get("description") or "")[1].replace(
                "\n", " "
            )
            age = f" ({result['age']})" if result.get("age") else ""
            lines.append(
                f"- [{result.get('title', '')}]({result.get('url', '')}){age}: {snippet}"
            )
        return "\n".join(lines)

    def _search_claude_code(self, query: str, count: int) -> str:
        from django_ergo.conversation.engines.claude_code import child_env
        from django_ergo.conversation.engines.claude_code import claude_command
        from django_ergo.conversation.engines.claude_code import workdir

        args = [
            claude_command(),
            *("-p", SEARCH_PROMPT.format(query=query, count=count)),
            *("--model", self.model or "haiku"),
            *("--output-format", "json"),
            *("--tools", "WebSearch"),
            *("--allowedTools", "WebSearch"),
            *("--permission-mode", "dontAsk"),
            *("--setting-sources", ""),
            "--strict-mcp-config",
            "--disable-slash-commands",
            *("--max-turns", "4"),
        ]
        try:
            proc = subprocess.run(  # noqa: S603 — the configured claude CLI
                args,
                cwd=workdir(),
                env=child_env(),
                capture_output=True,
                text=True,
                timeout=self.timeout,
                stdin=subprocess.DEVNULL,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            msg = f"claude CLI: {exc}"
            raise WebSearchError(msg) from exc
        try:
            reply = json.loads(proc.stdout)
        except ValueError:
            reply = {}
        if proc.returncode or reply.get("is_error") or not reply.get("result"):
            detail = reply.get("result") or proc.stderr.strip() or proc.stdout.strip()
            msg = f"claude CLI exited {proc.returncode}: {trim(detail, 500)}"
            raise WebSearchError(msg)
        return str(reply["result"]).strip()

    def _search_anthropic(self, query: str, count: int) -> str:
        key = os.environ.get(self.anthropic_key_env)
        if not key:
            msg = f"{self.anthropic_key_env} is not set"
            raise WebSearchError(msg)
        try:
            import anthropic
        except ImportError as exc:
            msg = "the anthropic package is not installed"
            raise WebSearchError(msg) from exc
        client = anthropic.Anthropic(api_key=key, timeout=self.timeout)
        try:
            response = client.messages.create(
                model=self.model or "claude-haiku-5-5",
                max_tokens=2048,
                tools=[
                    {"type": "web_search_20250305", "name": "web_search", "max_uses": 3}
                ],
                messages=[
                    {
                        "role": "user",
                        "content": SEARCH_PROMPT.format(query=query, count=count),
                    }
                ],
            )
        except anthropic.AnthropicError as exc:
            msg = f"Anthropic API: {exc}"
            raise WebSearchError(msg) from exc
        text = "".join(
            block.text
            for block in response.content
            if getattr(block, "type", "") == "text"
        ).strip()
        if text:
            return text
        sources = [
            f"- [{item.title}]({item.url})"
            for block in response.content
            if getattr(block, "type", "") == "web_search_tool_result"
            and isinstance(block.content, list)
            for item in block.content
        ]
        return "\n".join(sources[:count]) or f"No results for {query!r}."

    # -- fetch -------------------------------------------------------------

    def fetch(self, url: str) -> str:
        url = url.strip()
        if urllib.parse.urlparse(url).scheme not in ("http", "https"):
            return "Give an http or https URL."
        if not public_url(url):
            return f"Fetch refused: {url} is not a public address."
        request = urllib.request.Request(  # noqa: S310 — scheme checked above
            url,
            headers={"User-Agent": USER_AGENT, "Accept": "text/html,text/plain,*/*"},
        )
        opener = urllib.request.build_opener(_PublicRedirects)
        try:
            with opener.open(request, timeout=self.timeout) as response:
                kind = response.headers.get_content_type()
                charset = response.headers.get_content_charset() or "utf-8"
                body = response.read(5_000_000).decode(charset, errors="replace")
                final = response.geturl()
        except urllib.error.HTTPError as exc:
            return f"Fetch failed: HTTP {exc.code} {exc.reason} for {url}"
        except (urllib.error.URLError, TimeoutError, ValueError) as exc:
            return f"Fetch failed: {exc}"
        if kind in ("text/html", "application/xhtml+xml"):
            title, text = html_to_text(body)
        elif kind.startswith("text/") or kind in (
            "application/json",
            "application/xml",
        ):
            title, text = "", body
        else:
            return f"{final} is {kind}, not a text page."
        header = f"# {title}\n{final}" if title else final
        return trim(f"{header}\n\n{text or '(no text)'}", self.max_page_chars)

    # -- plugin hooks ------------------------------------------------------

    def toolkits(self, ctx: ToolContext) -> list[Toolkit]:
        return [FunctionToolkit(self._tools(), ctx)]

    def context_sources(self, ctx: ToolContext, message: str) -> list[ContextSource]:
        return [
            TextContextSource(
                "Web search",
                "ergo_web_search searches the web and ergo_web_fetch reads a page as "
                "text. Search when the answer depends on current or outside facts, "
                "fetch the best results before relying on them, and cite the URLs "
                "you used. Pages behind a sign-in need the browser instead.",
            )
        ]

    def _tools(self) -> list[BotTool]:
        plugin = self

        @bot_tool(
            name="ergo_web_search",
            description=(
                "Search the web. Returns the top results as markdown links with "
                "snippets. Use ergo_web_fetch to read a result."
            ),
            parameters={
                "query": {"type": "string", "description": "The search query"},
                "count": {
                    "type": "integer",
                    "description": f"How many results (default {self.count}, at most 20)",
                },
            },
            required=["query"],
        )
        def web_search(query: str, count: int = 0) -> str:
            return plugin.search(query, count)

        @bot_tool(
            name="ergo_web_fetch",
            description="Read a web page (http or https URL) as plain text.",
            parameters={"url": {"type": "string", "description": "The page URL"}},
            required=["url"],
        )
        def web_fetch(url: str) -> str:
            return plugin.fetch(url)

        return [web_search.__bot_tool__, web_fetch.__bot_tool__]
