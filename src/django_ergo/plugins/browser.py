"""Browser plugin: drive a running Chrome over the DevTools protocol (CDP).

    plugins:
      - name: browser
        cdp_url: http://127.0.0.1:9222   # Chrome's --remote-debugging-port (or a tunnel to it)
        takeover: the Chrome window on rigel  # where a person signs in for the bot
        approve_actions: true      # clicks, typing and key presses wait for approval
        root_only: true            # only the root session gets the tools
        timeout: 30                # seconds per browser call
        max_snapshot_chars: 20000

The bot attaches to a Chrome someone already started, with Playwright's
``connect_over_cdp``; it never launches one. That Chrome is an ordinary
headed browser with its own profile, so a person can sign in to sites for
the bot (Google, say) in the same window, and the bot sees the signed-in
tabs. Because Chrome isn't started by an automation driver, it doesn't
carry the automation flags that make sign-in pages refuse it.

Tools:

- ``ergo_browser_tabs``: the open tabs (short id, title, URL).
- ``ergo_browser_open``: go to a URL in the current tab or a new one.
- ``ergo_browser_snapshot``: the page as an accessibility tree, where each
  element the bot can act on has a ref (``[ref=e12]``).
- ``ergo_browser_click``, ``ergo_browser_type``, ``ergo_browser_press``:
  act on a ref (or any Playwright selector). They wait for approval unless
  ``approve_actions: false``, since the browser holds real sign-ins.
- ``ergo_browser_screenshot``: attach a screenshot of the tab to the chat.

Each call connects, works and disconnects; Chrome and its tabs keep
running. Refs belong to the snapshot that made them, so an action takes a
fresh snapshot first and resolves the ref against it, and returns the
page's new snapshot. The current tab is remembered per chat session.

Playwright is the ``browser`` extra (``pip install django-ergo[browser]``);
it needs no browser download, since it only connects. The DevTools port is
full control of the browser with no authentication: bind it to localhost
and reach it over an SSH tunnel or a private network, never the internet.
"""

from __future__ import annotations

import concurrent.futures
import threading
from typing import TYPE_CHECKING
from typing import Any

from django_ergo.bots.plugins import BotPlugin
from django_ergo.bots.tools import BotTool
from django_ergo.bots.tools import FunctionToolkit
from django_ergo.bots.tools import bot_tool
from django_ergo.conversation.context import TextContextSource
from django_ergo.plugins.bash import trim

if TYPE_CHECKING:
    from collections.abc import Callable

    from django_ergo.bots.tools import ToolContext
    from django_ergo.conversation.context import ContextSource
    from django_ergo.conversation.toolkit import Toolkit

TAB_ID_CHARS = 8


class BrowserPlugin(BotPlugin):
    name = "browser"
    description = "Drive a Chrome browser (open pages, read them, click and type)"

    def on_load(self) -> None:
        self.cdp_url = str(self.config.get("cdp_url") or "http://127.0.0.1:9222")
        self.takeover = str(self.config.get("takeover") or "the browser window")
        self.approve_actions = bool(self.config.get("approve_actions", True))
        self.root_only = bool(self.config.get("root_only", True))
        self.timeout = float(self.config.get("timeout", 30))
        self.max_snapshot_chars = int(self.config.get("max_snapshot_chars", 20_000))
        self._current: dict[str, str] = {}  # session key -> tab id
        self._lock = threading.Lock()

    # -- connection ---------------------------------------------------------

    def _run(self, fn: Callable[[Any], Any]) -> Any:
        """Connect to Chrome, call ``fn(browser_context)``, and disconnect.

        Playwright's sync API refuses to run in a thread with an event loop,
        so the work gets a thread of its own.
        """
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            msg = "The browser plugin needs Playwright: pip install 'django-ergo[browser]'"
            raise RuntimeError(msg) from exc

        def work() -> Any:
            with sync_playwright() as p:
                try:
                    browser = p.chromium.connect_over_cdp(
                        self.cdp_url, timeout=self.timeout * 1000
                    )
                except Exception as exc:
                    msg = (
                        f"Can't reach the browser at {self.cdp_url}: is Chrome running "
                        f"with --remote-debugging-port, and is the tunnel up? ({exc})"
                    )
                    raise RuntimeError(msg) from exc
                context = (
                    browser.contexts[0] if browser.contexts else browser.new_context()
                )
                context.set_default_timeout(self.timeout * 1000)
                return fn(context)

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(work).result()

    @staticmethod
    def _tab_id(page: Any) -> str:
        session = page.context.new_cdp_session(page)
        try:
            info = session.send("Target.getTargetInfo")
        finally:
            session.detach()
        return info["targetInfo"]["targetId"][:TAB_ID_CHARS].lower()

    def _pages(self, context: Any) -> list[tuple[str, Any]]:
        return [(self._tab_id(page), page) for page in context.pages]

    def _page(
        self, context: Any, key: str, tab: str = "", *, new: bool = False
    ) -> tuple[str, Any]:
        """The tab to work in: a named one, this chat's current one, or the first."""
        if new:
            page = context.new_page()
            return self._remember(key, self._tab_id(page)), page
        pages = self._pages(context)
        wanted = (tab or self._current.get(key, "")).lower()
        if wanted:
            for tab_id, page in pages:
                if tab_id.startswith(wanted) or wanted.startswith(tab_id):
                    return self._remember(key, tab_id), page
            if tab:
                msg = f"No tab {tab}; ergo_browser_tabs lists them."
                raise ValueError(msg)
        if pages:
            return self._remember(key, pages[0][0]), pages[0][1]
        page = context.new_page()
        return self._remember(key, self._tab_id(page)), page

    def _remember(self, key: str, tab_id: str) -> str:
        with self._lock:
            self._current[key] = tab_id
        return tab_id

    def _state(self, tab_id: str, page: Any) -> dict:
        page.wait_for_load_state()
        return {
            "tab": tab_id,
            "title": page.title(),
            "url": page.url,
            "snapshot": trim(page.aria_snapshot(mode="ai"), self.max_snapshot_chars),
        }

    @staticmethod
    def _locator(page: Any, target: str) -> Any:
        """A ref from the snapshot (e12, or [ref=e12]) or any Playwright selector."""
        ref = target.strip().removeprefix("[").removesuffix("]").removeprefix("ref=")
        if ref[:1] == "e" and ref[1:].isdigit():
            # Refs only resolve against a snapshot taken on this connection.
            page.aria_snapshot(mode="ai")
            return page.locator(f"aria-ref={ref}")
        return page.locator(target)

    @staticmethod
    def _key(ctx: ToolContext) -> str:
        return str(ctx.session.pk) if ctx.session is not None else ""

    # -- tool bodies --------------------------------------------------------

    def tabs(self, ctx: ToolContext) -> list[dict]:
        key = self._key(ctx)

        def work(context: Any) -> list[dict]:
            current = self._current.get(key, "")
            return [
                {
                    "tab": tab_id,
                    "title": page.title(),
                    "url": page.url,
                    **({"current": True} if tab_id == current else {}),
                }
                for tab_id, page in self._pages(context)
            ]

        return self._run(work)

    def open(
        self, ctx: ToolContext, url: str, tab: str = "", new_tab: bool = False
    ) -> dict:
        if not url.strip():
            msg = "Give a URL to open."
            raise ValueError(msg)
        if "://" not in url and not url.startswith(("about:", "data:")):
            url = f"https://{url}"
        key = self._key(ctx)

        def work(context: Any) -> dict:
            tab_id, page = self._page(context, key, tab, new=new_tab)
            page.goto(url)
            page.bring_to_front()
            return self._state(tab_id, page)

        return self._run(work)

    def snapshot(self, ctx: ToolContext, tab: str = "") -> dict:
        key = self._key(ctx)
        return self._run(lambda context: self._state(*self._page(context, key, tab)))

    def act(self, ctx: ToolContext, tab: str, action: Callable[[Any], None]) -> dict:
        key = self._key(ctx)

        def work(context: Any) -> dict:
            tab_id, page = self._page(context, key, tab)
            action(page)
            return self._state(tab_id, page)

        return self._run(work)

    def click(self, ctx: ToolContext, target: str, tab: str = "") -> dict:
        return self.act(ctx, tab, lambda page: self._locator(page, target).click())

    def type(
        self,
        ctx: ToolContext,
        target: str,
        text: str,
        submit: bool = False,
        tab: str = "",
    ) -> dict:
        def action(page: Any) -> None:
            field = self._locator(page, target)
            field.fill(text)
            if submit:
                field.press("Enter")

        return self.act(ctx, tab, action)

    def press(self, ctx: ToolContext, key: str, tab: str = "") -> dict:
        return self.act(ctx, tab, lambda page: page.keyboard.press(key))

    def screenshot(
        self, ctx: ToolContext, tab: str = "", full_page: bool = False
    ) -> dict:
        from datetime import UTC
        from datetime import datetime

        from django_ergo.conversation.attachments import save_session_file

        if ctx.session is None:
            msg = "Screenshots go into a chat"
            raise ValueError(msg)
        key = self._key(ctx)

        def work(context: Any) -> tuple[str, str, bytes]:
            tab_id, page = self._page(context, key, tab)
            return tab_id, page.url, page.screenshot(full_page=full_page)

        tab_id, url, data = self._run(work)
        stamp = datetime.now(tz=UTC).strftime("%Y%m%d-%H%M%S")
        row = save_session_file(
            ctx.session,
            f"browser-{stamp}.png",
            data,
            source="bot",
            metadata={"from_browser": {"tab": tab_id, "url": url}},
        )
        return {"id": str(row.id), "filename": row.filename, "size": row.size}

    # -- wiring -------------------------------------------------------------

    def _applies(self, ctx: ToolContext) -> bool:
        return not (self.root_only and ctx.bot and not ctx.bot.is_root(ctx.session))

    def toolkits(self, ctx: ToolContext) -> list[Toolkit]:
        if not self._applies(ctx):
            return []
        return [FunctionToolkit(self._tools(), ctx)]

    def context_sources(self, ctx: ToolContext, message: str) -> list[ContextSource]:
        if not self._applies(ctx):
            return []
        approval = (
            "Clicks, typing and key presses wait for the user's approval, so say what each does."
            if self.approve_actions
            else "Clicks, typing and key presses run without approval."
        )
        return [
            TextContextSource(
                "Browser",
                "The ergo_browser_ tools drive a real Chrome that stays open between calls, "
                "with the user's sign-ins. Read a page with ergo_browser_snapshot and act on "
                f"the refs it shows (e12). {approval} When a page needs a person (sign-in, "
                f"two-factor code, captcha, consent screen), stop and ask the user to do it in "
                f"{self.takeover}, then take a new snapshot once they say it's done. Never ask "
                "for passwords or codes in chat.",
            )
        ]

    def _tools(self) -> list[BotTool]:
        plugin = self
        tab_param = {
            "type": "string",
            "description": "Tab id from ergo_browser_tabs (default: this chat's current tab)",
        }

        @bot_tool(
            name="ergo_browser_tabs",
            takes_context=True,
            description="List the browser's open tabs: id, title and URL.",
            parameters={},
        )
        def tabs(ctx: ToolContext) -> list[dict]:
            return plugin.tabs(ctx)

        @bot_tool(
            name="ergo_browser_open",
            takes_context=True,
            description=(
                "Go to a URL in the current tab (or a new one) and return the page's "
                "title, URL and accessibility snapshot."
            ),
            parameters={
                "url": {"type": "string", "description": "The URL to open"},
                "new_tab": {"type": "boolean", "description": "Open it in a new tab"},
                "tab": tab_param,
            },
            required=["url"],
        )
        def open_url(
            ctx: ToolContext, url: str, new_tab: bool = False, tab: str = ""
        ) -> dict:
            return plugin.open(ctx, url, tab, new_tab)

        @bot_tool(
            name="ergo_browser_snapshot",
            takes_context=True,
            description=(
                "Read the page as an accessibility tree. Elements you can act on carry a "
                "ref like [ref=e12]."
            ),
            parameters={"tab": tab_param},
        )
        def snapshot(ctx: ToolContext, tab: str = "") -> dict:
            return plugin.snapshot(ctx, tab)

        target_param = {
            "type": "string",
            "description": "A ref from the latest snapshot (e12), or a Playwright selector",
        }

        @bot_tool(
            name="ergo_browser_click",
            takes_context=True,
            description="Click an element and return the page's new snapshot.",
            parameters={"target": target_param, "tab": tab_param},
            required=["target"],
            requires_approval=self.approve_actions,
        )
        def click(ctx: ToolContext, target: str, tab: str = "") -> dict:
            return plugin.click(ctx, target, tab)

        @bot_tool(
            name="ergo_browser_type",
            takes_context=True,
            description=(
                "Replace the text in a field, optionally press Enter, and return the "
                "page's new snapshot."
            ),
            parameters={
                "target": target_param,
                "text": {"type": "string", "description": "The text to enter"},
                "submit": {"type": "boolean", "description": "Press Enter afterwards"},
                "tab": tab_param,
            },
            required=["target", "text"],
            requires_approval=self.approve_actions,
        )
        def type_text(
            ctx: ToolContext,
            target: str,
            text: str,
            submit: bool = False,
            tab: str = "",
        ) -> dict:
            return plugin.type(ctx, target, text, submit, tab)

        @bot_tool(
            name="ergo_browser_press",
            takes_context=True,
            description=(
                "Press a key or chord in the page (Enter, Escape, PageDown, Control+A) and "
                "return the page's new snapshot."
            ),
            parameters={
                "key": {"type": "string", "description": "Playwright key name"},
                "tab": tab_param,
            },
            required=["key"],
            requires_approval=self.approve_actions,
        )
        def press(ctx: ToolContext, key: str, tab: str = "") -> dict:
            return plugin.press(ctx, key, tab)

        @bot_tool(
            name="ergo_browser_screenshot",
            takes_context=True,
            description=(
                "Screenshot a tab and attach it to this chat. Use ergo_attachments_look "
                "to see it yourself."
            ),
            parameters={
                "tab": tab_param,
                "full_page": {
                    "type": "boolean",
                    "description": "The whole page, not the view",
                },
            },
        )
        def screenshot(
            ctx: ToolContext, tab: str = "", full_page: bool = False
        ) -> dict:
            return plugin.screenshot(ctx, tab, full_page)

        return [
            t.__bot_tool__
            for t in (tabs, open_url, snapshot, click, type_text, press, screenshot)
        ]
