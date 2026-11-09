"""Browser plugin: drive a running Chrome over the DevTools protocol (CDP).

    plugins:
      - name: browser
        cdp_url: http://127.0.0.1:9222   # Chrome's --remote-debugging-port, as ssh_host sees it
        ssh_host: rigel            # optional: reach cdp_url through an ssh tunnel to this host
        takeover: the Chrome window on rigel  # where a person signs in for the bot
        sessions: true             # browser tools need a session opened with ergo_browser_start
        idle_minutes: 30           # a session with no browser calls this long ends
        launch_command: ""         # run (over ssh_host) at start when Chrome doesn't answer
        approve_actions: false     # clicks, typing and key presses also wait for approval
        root_only: true            # only the root session gets the tools
        timeout: 30                # seconds per browser call
        max_snapshot_chars: 20000
        driver_model: auto/small   # the model ergo_browser_task drives the browser with
        driver_steps: 30           # its tool calls per task, at most

The bot attaches to a Chrome someone already started, with Playwright's
``connect_over_cdp``. That Chrome is an ordinary headed browser with its own
profile, so a person can sign in to sites for the bot (Google, say) in the
same window, and the bot sees the signed-in tabs. Because Chrome isn't
started by an automation driver, it doesn't carry the automation flags that
make sign-in pages refuse it.

Tools:

- ``ergo_browser_start`` (always approved by the user): open a browser
  session for the bot. It shows in the chat as a Worker (``browser:session``)
  that checks Chrome still answers and ends after ``idle_minutes`` without a
  browser call. One session serves the whole bot, since there is one Chrome.
- ``ergo_browser_stop``: end the session, close the tabs it opened and the
  tunnel.
- ``ergo_browser_tabs``, ``ergo_browser_open``, ``ergo_browser_snapshot``
  (the page as an accessibility tree with refs like ``[ref=e12]``),
  ``ergo_browser_click``, ``ergo_browser_type``, ``ergo_browser_press`` and
  ``ergo_browser_screenshot``. With ``sessions: true`` they refuse to run
  until a session is open.
- ``ergo_browser_batch``: several clicks, typing, key presses or page loads
  in one call, with one snapshot at the end, for steps the bot already knows.
- ``ergo_browser_task``: hand a goal to a fast model (``driver_model``) that
  drives the tools above in its own loop and reports back, so a big model
  states the intent once instead of making a call per click. The driver
  stops before sign-ins, payments and anything that submits, publishes,
  saves or deletes unless the goal asks for it. With ``approve_actions`` the
  user approves the goal instead of each action.

Each call connects, works and disconnects; Chrome and its tabs keep running.
Refs belong to the snapshot that made them, so an action takes a fresh
snapshot first and resolves the ref against it, and returns the page's new
snapshot. Each chat's current tab is kept on the session.

With ``ssh_host``, a call reaches ``cdp_url`` through an ssh ControlMaster
that forwards a local port (``ssh -fN -M -L``, as the user Ergonaut runs as,
with its ssh config and keys). The master lives in the process's host or pod
and is reused by later calls there; a call in another pod opens its own.
Masters close with ``ergo_browser_stop`` (this pod's), at the next browser
call after the session ended, or after ``idle_minutes`` without use. Chrome
stays bound to the browser machine's localhost.

Playwright is the ``browser`` extra (``pip install django-ergo[browser]``);
it needs no browser download, since it only connects. The DevTools port is
full control of the browser with no authentication: bind it to localhost and
reach it over ssh or a private network, never the internet.
"""

from __future__ import annotations

import concurrent.futures
import contextlib
import fcntl
import hashlib
import re
import socket
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from dataclasses import field
from pathlib import Path
from typing import TYPE_CHECKING
from typing import Any
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel
from pydantic import Field

from django_ergo.bots.plugins import BotPlugin
from django_ergo.bots.tools import BotTool
from django_ergo.bots.tools import FunctionToolkit
from django_ergo.bots.tools import bot_tool
from django_ergo.conversation.context import TextContextSource
from django_ergo.plugins._common import trim
from django_ergo.protected_env import scrubbed_env

if TYPE_CHECKING:
    from collections.abc import Callable
    from collections.abc import Iterator

    from django_ergo.bots.tools import ToolContext
    from django_ergo.bots.workers import WorkerContext
    from django_ergo.conversation.context import ContextSource
    from django_ergo.conversation.models import Worker
    from django_ergo.conversation.toolkit import Toolkit

TAB_ID_CHARS = 8
WAIT_MAX_SECONDS = 300
REF = re.compile(r"(?:f\d+)?e\d+")
REF_IN_SNAPSHOT = re.compile(r"\[ref=(f\d+)?(e\d+)\]")
SESSION_FUNCTION = "browser:session"

DRIVER_PROMPT = """\
You drive a Chrome browser for another assistant, which gave you one goal. \
Work quickly with the ergo_browser_ tools: read the page (ergo_browser_snapshot), \
act on the refs it shows (e12), check the result in the snapshot each action \
returns, and repeat. Use ergo_browser_batch for several steps you're sure of. \
Don't explain as you go.

A person can watch and use this Chrome in {takeover}. When a page needs them \
(a sign-in, two-factor code, captcha or consent screen), they may already be \
on it: call ergo_browser_wait once (90 seconds) and carry on if the page moved \
on. If a tool says a tab moved since your last look, the person is using it: \
leave that tab and open what you need in a new one (ergo_browser_open with \
new_tab).

Stop and report instead of acting when:
- the page still needs a person after that wait;
- the next step would pay, buy, publish, submit, send, save or delete \
something and the goal doesn't ask for that step in so many words;
- you can't tell what the goal means here, or the page isn't what it expects.
Never type passwords or codes.

Report "done" only once the page shows the goal met (and the "done when" \
check, if given). Report "stopped" with what's needed when you stop for one \
of the reasons above, and "failed" when you tried and couldn't get there. The \
summary says what you did and what the page shows now, briefly."""


class TaskReport(BaseModel):
    status: Literal["done", "stopped", "failed"]
    summary: str = Field(description="What you did and what the page shows now")
    needs: str = Field(
        default="",
        description="When stopped: what a person or the assistant must do or decide",
    )


def fresh_refs(snapshot: str) -> str:
    """``snapshot``'s refs as the next connection will number them.

    Playwright prefixes refs with a frame number (``f2e5``) that it counts per
    connection: once a page has navigated, the main frame's refs carry one too.
    Each call connects anew, and a new connection numbers the main frame bare
    (``e5``) and the other frames ``f1``, ``f2``... in the order the snapshot
    reaches them. Renumber the same way, so the refs the bot sees are the ones
    the next call resolves.
    """
    frames: dict[str, str] = {}

    def renumber(match: re.Match) -> str:
        frame = match.group(1) or ""
        if frame not in frames:
            frames[frame] = f"f{len(frames)}" if frames else ""
        return f"[ref={frames[frame]}{match.group(2)}]"

    return REF_IN_SNAPSHOT.sub(renumber, snapshot)


@dataclass
class Use:
    """One call's view of a chat's tabs: its current tab, tabs it opened, and
    the URL the bot last saw in each tab (to notice a person using one)."""

    current: str = ""
    opened: list[str] = field(default_factory=list)
    seen: dict[str, str] = field(default_factory=dict)


class BrowserPlugin(BotPlugin):
    name = "browser"
    description = "Drive a Chrome browser (open pages, read them, click and type)"

    def on_load(self) -> None:
        self.cdp_url = str(self.config.get("cdp_url") or "http://127.0.0.1:9222")
        self.ssh_host = str(self.config.get("ssh_host") or "")
        self.takeover = str(self.config.get("takeover") or "the browser window")
        self.sessions = bool(self.config.get("sessions", True))
        self.idle_minutes = float(self.config.get("idle_minutes", 30))
        self.poll_seconds = float(self.config.get("poll_seconds", 120))
        self.launch_command = str(self.config.get("launch_command") or "")
        self.approve_actions = bool(self.config.get("approve_actions", False))
        self.root_only = bool(self.config.get("root_only", True))
        self.timeout = float(self.config.get("timeout", 30))
        self.max_snapshot_chars = int(self.config.get("max_snapshot_chars", 20_000))
        self.driver_model = str(self.config.get("driver_model") or "auto/small")
        self.driver_steps = int(self.config.get("driver_steps", 30))
        self._current: dict[str, str] = {}  # chat -> tab id, without sessions
        self._seen: dict[
            str, str
        ] = {}  # tab id -> URL the bot last saw, without sessions
        self._lock = threading.Lock()
        key = hashlib.sha1(  # noqa: S324 — a file name, not security
            f"{self.ssh_host}|{self.cdp_url}".encode()
        ).hexdigest()[:12]
        self._tunnel_dir = Path(tempfile.gettempdir()) / "ergo-browser"
        self._control = self._tunnel_dir / f"{key}.sock"
        self._port_file = self._tunnel_dir / f"{key}.port"

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
            with self._endpoint() as url, sync_playwright() as p:
                try:
                    browser = p.chromium.connect_over_cdp(
                        url, timeout=self.timeout * 1000
                    )
                except Exception as exc:
                    where = self.cdp_url + (
                        f" on {self.ssh_host}" if self.ssh_host else ""
                    )
                    msg = (
                        f"Can't reach the browser at {where}: is Chrome running "
                        f"with --remote-debugging-port? ({exc})"
                    )
                    raise RuntimeError(msg) from exc
                context = (
                    browser.contexts[0] if browser.contexts else browser.new_context()
                )
                context.set_default_timeout(self.timeout * 1000)
                return fn(context)

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(work).result()

    @contextlib.contextmanager
    def _endpoint(self) -> Iterator[str]:
        """``cdp_url``, or a local port the ssh master forwards to it."""
        if not self.ssh_host:
            yield self.cdp_url
            return
        scheme = urlsplit(self.cdp_url).scheme or "http"
        yield f"{scheme}://127.0.0.1:{self._tunnel_port()}"

    def _ssh(self, *args: str) -> subprocess.CompletedProcess:
        # Output goes to a file, not a pipe: ``ssh -f`` keeps stderr open in the
        # background master, so reading a pipe to its end would never finish.
        with tempfile.TemporaryFile("w+") as out:
            proc = subprocess.run(  # noqa: S603 — argv list, no shell
                ["ssh", "-o", "BatchMode=yes", *args],  # noqa: S607
                stdin=subprocess.DEVNULL,
                stdout=out,
                stderr=out,
                timeout=self.timeout,
                check=False,
                env=scrubbed_env(),
            )
            out.seek(0)
            return subprocess.CompletedProcess(
                proc.args, proc.returncode, "", out.read()
            )

    def _master_alive(self) -> bool:
        if not self._control.exists():
            return False
        check = self._ssh("-S", str(self._control), "-O", "check", self.ssh_host)
        return check.returncode == 0

    def _tunnel_port(self) -> int:
        """The local port of this host's ssh master, started if it isn't running."""
        self._tunnel_dir.mkdir(mode=0o700, exist_ok=True)
        with (self._tunnel_dir / "lock").open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if self._master_alive():
                with contextlib.suppress(ValueError, OSError):
                    return int(self._port_file.read_text())
                self.close_tunnel()
            target = urlsplit(self.cdp_url)
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                port = sock.getsockname()[1]
            started = self._ssh(
                "-fN",
                "-o",
                "ExitOnForwardFailure=yes",
                "-o",
                "ControlMaster=yes",
                "-o",
                f"ControlPersist={max(1, int(self.idle_minutes))}m",
                "-S",
                str(self._control),
                "-L",
                f"127.0.0.1:{port}:{target.hostname}:{target.port or 80}",
                self.ssh_host,
            )
            if started.returncode != 0:
                error = started.stderr.strip()[:500]
                msg = f"ssh {self.ssh_host} failed: {error or f'exit {started.returncode}'}"
                raise RuntimeError(msg)
            self._port_file.write_text(str(port))
            return port

    def close_tunnel(self) -> bool:
        """Stop this host's ssh master, if one runs. True if one did."""
        if not self.ssh_host or not self._control.exists():
            return False
        self._ssh("-S", str(self._control), "-O", "exit", self.ssh_host)
        self._port_file.unlink(missing_ok=True)
        return True

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
        self, context: Any, use: Use, tab: str = "", *, new: bool = False
    ) -> tuple[str, Any]:
        """The tab to work in: a named one, this chat's current one, or the first."""
        if new:
            page = context.new_page()
            use.current = self._tab_id(page)
            use.opened.append(use.current)
            return use.current, page
        pages = self._pages(context)
        wanted = (tab or use.current).lower()
        if wanted:
            for tab_id, page in pages:
                if tab_id.startswith(wanted) or wanted.startswith(tab_id):
                    use.current = tab_id
                    return tab_id, page
            if tab:
                msg = f"No tab {tab}; ergo_browser_tabs lists them."
                raise ValueError(msg)
        if pages:
            use.current = pages[0][0]
            return pages[0]
        page = context.new_page()
        use.current = self._tab_id(page)
        use.opened.append(use.current)
        return use.current, page

    def _state(self, tab_id: str, page: Any, use: Use | None = None) -> dict:
        page.wait_for_load_state()
        if use is not None:
            use.seen[tab_id] = page.url
        return {
            "tab": tab_id,
            "title": page.title(),
            "url": page.url,
            "snapshot": trim(
                fresh_refs(page.aria_snapshot(mode="ai")), self.max_snapshot_chars
            ),
        }

    @staticmethod
    def _locator(page: Any, target: str) -> Any:
        """A ref from the snapshot (e12, or [ref=e12]) or any Playwright selector."""
        ref = target.strip().removeprefix("[").removesuffix("]").removeprefix("ref=")
        if REF.fullmatch(ref):
            # Refs only resolve against a snapshot taken on this connection.
            page.aria_snapshot(mode="ai")
            return page.locator(f"aria-ref={ref}")
        return page.locator(target)

    @staticmethod
    def _key(ctx: ToolContext) -> str:
        return str(ctx.session.pk) if ctx.session is not None else ""

    # -- sessions -----------------------------------------------------------

    def session(self) -> Worker | None:
        """The bot's open browser session (a Worker), if any."""
        from django_ergo.conversation.models import Worker

        return (
            Worker.objects.filter(
                bot_name=self.bot.name,
                function=SESSION_FUNCTION,
                status__in=["queued", "running"],
            )
            .order_by("-created_at")
            .first()
        )

    @contextlib.contextmanager
    def _use(self, ctx: ToolContext) -> Iterator[Use]:
        """This chat's tab bookkeeping for one call, saved on the session."""
        key = self._key(ctx)
        if not self.sessions:
            use = Use(current=self._current.get(key, ""), seen=dict(self._seen))
            yield use
            with self._lock:
                self._current[key] = use.current
                self._seen.update(use.seen)
            return
        worker = self.session()
        if worker is None:
            self.close_tunnel()
            msg = "No browser session is open; start one with ergo_browser_start."
            raise RuntimeError(msg)
        use = Use(
            current=(worker.state.get("current") or {}).get(key, ""),
            seen=dict(worker.state.get("seen") or {}),
        )
        try:
            yield use
        finally:
            self._save(worker.pk, key, use)

    @staticmethod
    def _save(worker_id: Any, key: str, use: Use) -> None:
        from django.db import transaction

        from django_ergo.conversation.models import Worker

        with transaction.atomic():
            worker = Worker.objects.select_for_update().filter(pk=worker_id).first()
            if worker is None:
                return
            state = dict(worker.state or {})
            state["current"] = {**(state.get("current") or {}), key: use.current}
            state["opened"] = [*(state.get("opened") or []), *use.opened]
            state["seen"] = {**(state.get("seen") or {}), **use.seen}
            state["used_at"] = time.time()
            worker.state = state
            worker.save(update_fields=["state", "updated_at"])

    def start(self, ctx: ToolContext) -> str:
        from django_ergo.bots import workers

        if ctx.session is None:
            msg = "A browser session belongs to a chat"
            raise ValueError(msg)
        where = self._where()
        current = self.session()
        if current is not None:
            return f"A browser session is already open ({current.title})."
        launched = ""
        try:
            count = self._run(lambda context: len(context.pages))
        except RuntimeError:
            if not self.launch_command:
                raise
            launched = self._launch()
            count = self._run(lambda context: len(context.pages))
        workers.start(
            self.bot,
            ctx.session,
            SESSION_FUNCTION,
            {},
            title=f"Browser on {where}",
            notify=False,
            state={"used_at": time.time(), "current": {}, "opened": []},
        )
        return (
            f"{launched}Browser session open on {where}, {count} tab(s). It ends after "
            f"{self.idle_minutes:g} idle minutes or with ergo_browser_stop."
        )

    def _launch(self) -> str:
        """Run ``launch_command`` (over ssh_host) and wait for Chrome to answer."""
        if self.ssh_host:
            proc = self._ssh(self.ssh_host, self.launch_command)
        else:
            proc = subprocess.run(  # noqa: S603 — the configured command
                ["bash", "-lc", self.launch_command],  # noqa: S607
                capture_output=True,
                text=True,
                stdin=subprocess.DEVNULL,
                timeout=self.timeout,
                check=False,
                env=scrubbed_env(),
            )
        if proc.returncode != 0:
            msg = f"launch_command failed: {(proc.stderr or proc.stdout or '').strip()[:500]}"
            raise RuntimeError(msg)
        deadline = time.monotonic() + self.timeout
        error: RuntimeError | None = None
        while time.monotonic() < deadline:
            try:
                self._run(lambda context: len(context.pages))
            except RuntimeError as exc:
                error = exc
            else:
                return "Started Chrome. "
            time.sleep(1)
        # The last connection error says whether ssh, the tunnel or Chrome failed.
        msg = f"launch_command ran, but Chrome didn't answer: {error}"
        raise RuntimeError(msg) from error

    def stop(self, ctx: ToolContext, close_tabs: bool = True) -> str:
        from django_ergo.bots import workers

        worker = self.session()
        if worker is None:
            self.close_tunnel()
            return "No browser session is open."
        closed = self._close_tabs(worker.state.get("opened") or []) if close_tabs else 0
        workers.cancel(worker)
        self.close_tunnel()
        return f"Browser session ended; closed {closed} tab(s) it opened."

    def _close_tabs(self, tab_ids: list[str]) -> int:
        def work(context: Any) -> int:
            closed = 0
            for tab_id, page in self._pages(context):
                if tab_id in tab_ids:
                    page.close()
                    closed += 1
            return closed

        try:
            return self._run(work)
        except RuntimeError:
            return 0

    def watch(self, ctx: WorkerContext) -> Any:
        """The session Worker: check Chrome answers, end the session when idle."""
        from django_ergo.conversation.models import Worker

        fresh = Worker.objects.filter(pk=ctx.worker.pk).values_list("state", flat=True)
        ctx.state = dict(fresh.first() or ctx.state)
        if ctx.stopping:
            return None
        idle = time.time() - float(ctx.state.get("used_at") or 0)
        if idle > self.idle_minutes * 60:
            closed = self._close_tabs(ctx.state.get("opened") or [])
            self.close_tunnel()
            return f"Browser session ended after {self.idle_minutes:g} idle minutes; closed {closed} tab(s)."
        try:
            count = self._run(lambda context: len(context.pages))
            line = f"Chrome on {self._where()}: {count} tab(s)"
        except RuntimeError as exc:
            line = str(exc)[:300]
        # The step saves ctx.state when it ends: take in what browser calls
        # recorded while Chrome was being checked.
        fresh = Worker.objects.filter(pk=ctx.worker.pk).values_list("state", flat=True)
        ctx.state = dict(fresh.first() or ctx.state)
        return ctx.again(self.poll_seconds, line)

    def worker_functions(self) -> dict[str, Callable]:
        return {"session": self.watch}

    def _where(self) -> str:
        return self.ssh_host or urlsplit(self.cdp_url).netloc

    # -- tool bodies --------------------------------------------------------

    def tabs(self, ctx: ToolContext) -> list[dict]:
        with self._use(ctx) as use:
            return self._run(
                lambda context: [
                    {
                        "tab": tab_id,
                        "title": page.title(),
                        "url": page.url,
                        **({"current": True} if tab_id == use.current else {}),
                    }
                    for tab_id, page in self._pages(context)
                ]
            )

    def open(
        self, ctx: ToolContext, url: str, tab: str = "", new_tab: bool = False
    ) -> dict:
        if not url.strip():
            msg = "Give a URL to open."
            raise ValueError(msg)
        if "://" not in url and not url.startswith(("about:", "data:")):
            url = f"https://{url}"
        with self._use(ctx) as use:

            def work(context: Any) -> dict:
                tab_id, page = self._page(context, use, tab, new=new_tab)
                page.goto(url)
                page.bring_to_front()
                return self._state(tab_id, page, use)

            return self._run(work)

    def snapshot(self, ctx: ToolContext, tab: str = "") -> dict:
        with self._use(ctx) as use:

            def work(context: Any) -> dict:
                tab_id, page = self._page(context, use, tab)
                moved = self._moved(use, tab_id, page)
                state = self._state(tab_id, page, use)
                if moved:
                    state["note"] = (
                        f"This tab was at {moved} when you last looked; someone may be "
                        "using it. If they are, leave it to them and work in a new tab."
                    )
                return state

            return self._run(work)

    @staticmethod
    def _moved(use: Use, tab_id: str, page: Any) -> str:
        """The URL the bot last saw in this tab, if the tab has gone elsewhere since."""
        last = use.seen.get(tab_id, "")
        if last and last.partition("#")[0] != page.url.partition("#")[0]:
            return last
        return ""

    def _check_not_moved(self, use: Use, tab_id: str, page: Any) -> None:
        if moved := self._moved(use, tab_id, page):
            msg = (
                f"Tab {tab_id} moved from {moved} to {page.url} since your last look, "
                "so the user may be using it. Leave it to them: open what you need in a "
                "new tab (ergo_browser_open with new_tab), or take a snapshot first if "
                "you mean to carry on in this one."
            )
            raise RuntimeError(msg)

    def act(self, ctx: ToolContext, tab: str, action: Callable[[Any], None]) -> dict:
        with self._use(ctx) as use:

            def work(context: Any) -> dict:
                tab_id, page = self._page(context, use, tab)
                self._check_not_moved(use, tab_id, page)
                action(page)
                return self._state(tab_id, page, use)

            return self._run(work)

    def wait(
        self, ctx: ToolContext, seconds: float = 60, until: str = "", tab: str = ""
    ) -> dict:
        """Wait while a person uses the tab, until it moves on or ``until`` shows."""
        seconds = max(1.0, min(float(seconds), WAIT_MAX_SECONDS))
        with self._use(ctx) as use:

            def work(context: Any) -> dict:
                tab_id, page = self._page(context, use, tab)
                start = page.url
                deadline = time.monotonic() + seconds
                done = False
                while time.monotonic() < deadline and not done:
                    page.wait_for_timeout(2000)
                    with contextlib.suppress(Exception):  # mid-navigation reads fail
                        if until:
                            done = (
                                until.lower()
                                in (
                                    page.url
                                    + " "
                                    + page.title()
                                    + " "
                                    + page.inner_text("body")
                                ).lower()
                            )
                        else:
                            done = page.url != start
                return {**self._state(tab_id, page, use), "waited_until_done": done}

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

    def batch(self, ctx: ToolContext, steps: list[dict], tab: str = "") -> dict:
        """Run ``steps`` in order on one connection; stop at the first that fails."""
        if not steps:
            msg = "Give at least one step."
            raise ValueError(msg)
        with self._use(ctx) as use:

            def work(context: Any) -> dict:
                tab_id, page = self._page(context, use, tab)
                self._check_not_moved(use, tab_id, page)
                for number, step in enumerate(steps, 1):
                    try:
                        self._step(page, step)
                    except Exception as exc:  # noqa: BLE001 — reported with the page
                        return {
                            **self._state(tab_id, page, use),
                            "done": number - 1,
                            "failed": {"step": number, "error": str(exc)[:500]},
                        }
                return {**self._state(tab_id, page, use), "done": len(steps)}

            return self._run(work)

    def _step(self, page: Any, step: dict) -> None:
        action = step.get("action")
        if action == "click":
            self._locator(page, step["target"]).click()
        elif action == "type":
            field = self._locator(page, step["target"])
            field.fill(str(step.get("text", "")))
            if step.get("submit"):
                field.press("Enter")
        elif action == "press":
            page.keyboard.press(step["key"])
        elif action == "open":
            url = str(step["url"])
            page.goto(url if "://" in url else f"https://{url}")
        elif action == "wait":
            page.wait_for_timeout(min(float(step.get("seconds", 1)), 10) * 1000)
        else:
            msg = f"Unknown action {action!r}: use click, type, press, open or wait"
            raise ValueError(msg)

    def task(
        self, ctx: ToolContext, goal: str, done_when: str = "", tab: str = ""
    ) -> dict:
        """Have ``driver_model`` drive the browser toward ``goal`` and report."""
        from asgiref.sync import async_to_sync

        from django_ergo.conversation.structured import StructuredCallSpec
        from django_ergo.conversation.structured import run_structured_call

        if not goal.strip():
            msg = "Say what the browser should get done."
            raise ValueError(msg)
        with self._use(ctx):
            pass  # refuse before spending a model call when no session is open
        request = f"Goal: {goal.strip()}"
        if done_when.strip():
            request += f"\nDone when: {done_when.strip()}"
        if tab:
            request += f"\nWork in tab {tab}."
        spec = StructuredCallSpec(
            kind="browser_task",
            system_prompt=DRIVER_PROMPT.format(takeover=self.takeover),
            response_model=TaskReport,
            toolkits=[FunctionToolkit(self._tools(driving=True), ctx)],
            max_turns=self.driver_steps,
            wrap_up=True,
        )
        started = time.monotonic()
        result = async_to_sync(run_structured_call)(
            spec,
            request,
            user=ctx.user,
            engine=self.bot.make_engine(
                ctx.session, self.driver_model, purpose="browser"
            ),
            metadata={"session": str(ctx.session.pk) if ctx.session else ""},
        )
        report = result.parsed
        out: dict = {
            "status": report.status if report else "failed",
            "summary": report.summary if report else (result.call.error or "no report"),
            "seconds": round(time.monotonic() - started, 1),
        }
        if report and report.needs:
            out["needs"] = report.needs
        with contextlib.suppress(RuntimeError, ValueError):
            out["page"] = self.snapshot(ctx, tab)
        return out

    def screenshot(
        self, ctx: ToolContext, tab: str = "", full_page: bool = False
    ) -> dict:
        from datetime import UTC
        from datetime import datetime

        from django_ergo.conversation.attachments import save_session_file

        if ctx.session is None:
            msg = "Screenshots go into a chat"
            raise ValueError(msg)
        with self._use(ctx) as use:

            def work(context: Any) -> tuple[str, str, bytes]:
                tab_id, page = self._page(context, use, tab)
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

    def toolkits(self, ctx: ToolContext) -> list[Toolkit]:
        if not self._applies(ctx):
            return []
        return [FunctionToolkit(self._tools(), ctx)]

    def context_sources(self, ctx: ToolContext, message: str) -> list[ContextSource]:
        if not self._applies(ctx):
            return []
        approval = (
            "Clicks, typing, key presses, batches and tasks wait for the user's approval, "
            "so say what each does."
            if self.approve_actions
            else "Clicks, typing and key presses run without approval."
        )
        session = (
            "Open a session with ergo_browser_start first (the user approves it) and end "
            "it with ergo_browser_stop when you're done. "
            if self.sessions
            else ""
        )
        return [
            TextContextSource(
                "Browser",
                f"{session}"
                "The ergo_browser_ tools drive a real Chrome that stays open between calls, "
                "with the user's sign-ins. Browse with ergo_browser_task by default: give it "
                "the goal once (search a site, fill a form, find and read pages, collect "
                "links) and a fast model does the clicking and typing and reports back. "
                "Each click you make yourself costs a full model turn, so don't drive a "
                "page step by step. Use ergo_browser_open and ergo_browser_snapshot to "
                "look at a page, ergo_browser_batch for steps you already know, and the "
                "single click, type and press tools only for one action or to finish what "
                f"a task stopped at. Refs (e12, f1e3) come from the latest snapshot. "
                f"{approval}\n\nThe user can watch and use this Chrome in {self.takeover}, "
                "and may take over a tab. When a page needs them (a sign-in, two-factor "
                "code, captcha or consent screen), say so in your reply, then call "
                "ergo_browser_wait (60 to 180 seconds) so they can do it while you wait, and "
                "carry on once the page moves on; if it doesn't, end your turn asking them "
                "to tell you when it's done. If a tab moved since your last look (a tool "
                "says so), the user is using it: leave it to them and open what you need in "
                "a new tab with ergo_browser_open new_tab. Never ask for passwords or codes "
                "in chat.",
            )
        ]

    def _tools(self, *, driving: bool = False) -> list[BotTool]:  # noqa: C901
        """The browser tools; ``driving`` is the set ergo_browser_task's model gets.

        A task's goal was approved as a whole, so its actions need no approval.
        """
        plugin = self
        approve = self.approve_actions and not driving
        tab_param = {
            "type": "string",
            "description": "Tab id from ergo_browser_tabs (default: this chat's current tab)",
        }

        @bot_tool(
            name="ergo_browser_start",
            takes_context=True,
            description=(
                f"Open a browser session on {self._where()} (the user approves it). The "
                "other browser tools work only while one is open."
            ),
            parameters={},
            requires_approval=True,
        )
        def start(ctx: ToolContext) -> str:
            return plugin.start(ctx)

        @bot_tool(
            name="ergo_browser_stop",
            takes_context=True,
            description="End the browser session: close the tabs it opened and the tunnel.",
            parameters={
                "close_tabs": {
                    "type": "boolean",
                    "description": "Close the tabs the session opened (default true)",
                }
            },
        )
        def stop(ctx: ToolContext, close_tabs: bool = True) -> str:
            return plugin.stop(ctx, close_tabs)

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
            description=(
                "Click one element and return the page's new snapshot. For more than "
                "one action, use ergo_browser_task or ergo_browser_batch."
            ),
            parameters={"target": target_param, "tab": tab_param},
            required=["target"],
            requires_approval=approve,
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
            requires_approval=approve,
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
            requires_approval=approve,
        )
        def press(ctx: ToolContext, key: str, tab: str = "") -> dict:
            return plugin.press(ctx, key, tab)

        @bot_tool(
            name="ergo_browser_wait",
            takes_context=True,
            description=(
                "Wait while the user does something in the browser (signs in, enters a "
                "code, solves a captcha), up to `seconds`. Returns as soon as the tab "
                "goes to another page, or `until` text shows on it, with the new "
                "snapshot. Tell the user what you're waiting for first."
            ),
            parameters={
                "seconds": {
                    "type": "number",
                    "description": f"At most this long (default 60, max {WAIT_MAX_SECONDS})",
                },
                "until": {
                    "type": "string",
                    "description": "Text to wait for in the page, title or URL (default: any change of page)",
                },
                "tab": tab_param,
            },
        )
        def wait(
            ctx: ToolContext, seconds: float = 60, until: str = "", tab: str = ""
        ) -> dict:
            return plugin.wait(ctx, seconds, until, tab)

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

        @bot_tool(
            name="ergo_browser_batch",
            takes_context=True,
            description=(
                "Run several steps in one call and return the page's snapshot after the "
                "last. Stops at the first step that fails and says which. Refs come from "
                "the latest snapshot; after a step loads a new page, use selectors or "
                "take a new snapshot."
            ),
            parameters={
                "steps": {
                    "type": "array",
                    "description": (
                        'In order, each {"action": "click", "target": ...}, {"action": '
                        '"type", "target": ..., "text": ..., "submit": false}, {"action": '
                        '"press", "key": ...}, {"action": "open", "url": ...} or '
                        '{"action": "wait", "seconds": 1}'
                    ),
                    "items": {"type": "object"},
                },
                "tab": tab_param,
            },
            required=["steps"],
            requires_approval=approve,
        )
        def batch(ctx: ToolContext, steps: list[dict], tab: str = "") -> dict:
            return plugin.batch(ctx, steps, tab)

        @bot_tool(
            name="ergo_browser_task",
            takes_context=True,
            description=(
                "The default way to browse: hand the browser a goal and a fast model "
                "drives it (opening pages, reading, clicking, typing) until the goal is "
                "met, then reports with the final page. One call instead of one per click. It stops and says "
                "so before sign-ins, payments, or submitting, saving, publishing or "
                "deleting anything the goal doesn't ask for, so name those steps if you "
                "want them done. Check its report against the page."
            ),
            parameters={
                "goal": {
                    "type": "string",
                    "description": "What to get done, with the details it needs (names, values, URLs)",
                },
                "done_when": {
                    "type": "string",
                    "description": "What the page shows once it's done",
                },
                "tab": tab_param,
            },
            required=["goal"],
            requires_approval=approve,
        )
        def task(
            ctx: ToolContext, goal: str, done_when: str = "", tab: str = ""
        ) -> dict:
            return plugin.task(ctx, goal, done_when, tab)

        if driving:
            return [
                t.__bot_tool__
                for t in (
                    tabs,
                    open_url,
                    snapshot,
                    click,
                    type_text,
                    press,
                    batch,
                    wait,
                )
            ]
        browsing = (
            tabs,
            open_url,
            snapshot,
            click,
            type_text,
            press,
            wait,
            batch,
            task,
            screenshot,
        )
        sessions = (start, stop) if self.sessions else ()
        return [t.__bot_tool__ for t in (*sessions, *browsing)]
