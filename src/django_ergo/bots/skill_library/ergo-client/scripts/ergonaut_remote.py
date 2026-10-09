#!/usr/bin/env python3
"""ergonaut-remote: a command-line client for an Ergonaut server's API.

Standard library only, so it runs anywhere Python 3.10+ does (Claude Code,
Codex, a bot's shell), with no Ergonaut install. Where Ergonaut is installed,
``ergonaut remote ...`` runs the same commands. Servers and keys live in
~/.config/ergonaut/remote.json (ERGONAUT_REMOTE_CONFIG to move it);
ERGONAUT_URL and ERGONAUT_API_KEY override the saved server.

    ergonaut-remote login https://ergo.example.com --name prod     # asks for an API key
    ergonaut-remote bots                                           # bots you can use
    ergonaut-remote new devbox "Fix the flaky upload test" --wait  # start a thread, wait for the reply
    ergonaut-remote threads --bucket waiting                       # threads waiting on you
    ergonaut-remote show <session>                                 # transcript, workers, approvals
    ergonaut-remote show https://ergo.example.com/s/<id>#m-12      # a linked message, with the ones around it
    ergonaut-remote send <session> "Yes, go ahead" --wait
    ergonaut-remote approve <session>                              # or --deny

Make a key in the web app (API keys, at the bottom of the sidebar) or with
``ergonaut manage api_key create <user> --name <where>``. Run ``ergonaut-remote -h`` or
``ergonaut-remote <command> -h`` for everything else; ``--json`` prints raw API output.
"""

from __future__ import annotations

import argparse
import contextlib
import contextvars
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

CONFIG = Path(
    os.environ.get(
        "ERGONAUT_REMOTE_CONFIG", Path.home() / ".config" / "ergonaut" / "remote.json"
    )
)
TERMINAL = {
    "completed",
    "failed",
    "awaiting_approval",
    "cancelled",
    "error",
    "max_turns",
}


# Where output goes: a stream set by a caller that wants it as text (the bot tools), else the terminal.
OUTPUT: contextvars.ContextVar = contextvars.ContextVar("ergo_output", default=None)


def echo(*parts, **kwargs) -> None:
    stream = OUTPUT.get()
    if stream is not None:
        kwargs["file"] = stream
    print(*parts, **kwargs)


def end_progress() -> None:
    """End the line of progress dots wait_for_turn printed on a terminal."""
    if OUTPUT.get() is None:
        print(file=sys.stderr)


class ApiError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(f"HTTP {status}: {detail}")
        self.status = status
        self.detail = detail


# -- configuration -------------------------------------------------------------


def load_config() -> dict:
    try:
        return json.loads(CONFIG.read_text())
    except FileNotFoundError:
        return {"default": "", "servers": {}}


def save_config(config: dict) -> None:
    CONFIG.parent.mkdir(parents=True, exist_ok=True)
    CONFIG.write_text(json.dumps(config, indent=2) + "\n")
    CONFIG.chmod(0o600)


def server(name: str = "", link: str = "") -> tuple[str, str]:
    """The (url, key) to use: ERGONAUT_URL/ERGONAUT_API_KEY, else the named server, else the
    saved server a link (https://host/s/...) points at, else the default one."""
    if (
        os.environ.get("ERGONAUT_URL")
        and os.environ.get("ERGONAUT_API_KEY")
        and not name
    ):
        return os.environ["ERGONAUT_URL"].rstrip("/"), os.environ["ERGONAUT_API_KEY"]
    config = load_config()
    host = urllib.parse.urlsplit(link).netloc if "://" in link else ""
    if host and not name and not os.environ.get("ERGONAUT_SERVER"):
        for found in config.get("servers", {}).values():
            if urllib.parse.urlsplit(found["url"]).netloc == host:
                return found["url"].rstrip("/"), found["key"]
    name = name or os.environ.get("ERGONAUT_SERVER") or config.get("default", "")
    found = config.get("servers", {}).get(name)
    if not found:
        known = ", ".join(config.get("servers", {})) or "none"
        sys.exit(
            f"No Ergonaut server {name!r} (saved: {known}). Run: ergonaut-remote login URL --name NAME"
        )
    return found["url"].rstrip("/"), found["key"]


# -- HTTP ------------------------------------------------------------------------


class Client:
    def __init__(self, url: str, key: str, timeout: float = 120):
        self.url, self.key, self.timeout = url.rstrip("/"), key, timeout

    def call(self, method: str, path: str, body=None, **query):
        query = {k: v for k, v in query.items() if v not in (None, "")}
        url = f"{self.url}/api{path}" + (
            f"?{urllib.parse.urlencode(query)}" if query else ""
        )
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(url, data=data, method=method)
        request.add_header("Authorization", f"Bearer {self.key}")
        request.add_header("Accept", "application/json")
        if data is not None:
            request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read()
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")
            with contextlib.suppress(ValueError, AttributeError):
                detail = json.loads(detail).get("detail", detail)
            raise ApiError(e.code, str(detail)[:500]) from None
        except urllib.error.URLError as e:
            raise ApiError(0, f"can't reach {self.url}: {e.reason}") from None
        return json.loads(raw) if raw else None

    def get(self, path, **query):
        return self.call("GET", path, **query)

    def post(self, path, body=None):
        return self.call("POST", path, body if body is not None else {})


# -- the conversation helpers the commands share ------------------------------------


def chat_calls(detail: dict) -> list[dict]:
    return [c for c in detail.get("calls", []) if c.get("kind") == "chat_reply"]


def snapshot(client: Client, session_id: str) -> dict:
    """{call id: status} of the session's chat replies, to tell a new turn from old ones."""
    detail = client.get(f"/sessions/{session_id}", limit=5)
    return {c["id"]: c["status"] for c in chat_calls(detail)}


def wait_for_turn(
    client: Client, session_id: str, before: dict, timeout: float, quiet: bool = False
) -> dict | None:
    """Poll until a turn started after ``before`` (or one running or waiting then) has finished
    and nothing is left in the inbox. Returns that chat reply call, or None on timeout."""
    deadline = time.monotonic() + timeout
    delay = 2.0
    while True:
        detail = client.get(f"/sessions/{session_id}", limit=5)
        calls = chat_calls(detail)
        # New calls, plus ones that were running (or waiting on an approval) and moved since.
        ours = [
            c
            for c in calls
            if before.get(c["id"]) in (None, "in_progress")
            or before[c["id"]] != c["status"]
        ]
        running = any(c["status"] == "in_progress" for c in calls)
        finished = [c for c in ours if c["status"] != "in_progress"]
        if finished and not running and not detail.get("inbox"):
            return max(finished, key=lambda c: c["created_at"])
        if time.monotonic() > deadline:
            return None
        if not quiet and OUTPUT.get() is None:
            echo(".", end="", file=sys.stderr, flush=True)
        time.sleep(delay)
        delay = min(delay * 1.5, 10)


def print_reply(call: dict, session_id: str) -> None:
    response = call.get("response") or {}
    if isinstance(response, dict) and response.get("text"):
        echo(response["text"])
    if call.get("status") == "awaiting_approval":
        echo("\nWaiting for approval:")
        for item in call.get("pending_approvals") or []:
            echo(
                f"  - {item.get('name')} {json.dumps(item.get('input', item.get('arguments', {})))[:300]}"
            )
        echo(f"Answer with: ergonaut-remote approve {session_id}   (or --deny)")
    elif call.get("status") != "completed":
        echo(
            f"\n[{call.get('status')}] {call.get('error_summary') or call.get('error') or ''}".rstrip()
        )
        if call.get("error_hint"):
            echo(call["error_hint"])
    if isinstance(response, dict) and response.get("suggestions"):
        echo("\nSuggested replies: " + " | ".join(response["suggestions"]))


def send_and_maybe_wait(client: Client, session_id: str, text: str, args) -> int:
    before = snapshot(client, session_id) if args.wait else {}
    turn = client.post(
        f"/sessions/{session_id}/messages",
        {"text": text, "mode": "interrupt" if args.interrupt else "send"},
    )
    if not args.wait:
        if args.json:
            echo(json.dumps(turn, indent=2))
        else:
            echo(
                f"Sent to {session_id}."
                + (" A worker runs the turn." if turn.get("queued") else "")
            )
        return 0
    if not turn.get("queued") and turn.get("call_id"):
        before.pop(turn["call_id"], None)  # ran inline (no broker): it's already done
    call = wait_for_turn(client, session_id, before, args.timeout, quiet=args.json)
    if not args.json:
        end_progress()
    if call is None:
        echo(
            f"Still working after {args.timeout:.0f}s. Check later: ergonaut-remote show {session_id}",
            file=sys.stderr,
        )
        return 3
    if args.json:
        echo(json.dumps(call, indent=2))
    else:
        print_reply(call, session_id)
    return 0


LINK = re.compile(
    r"/s/([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})[^#\s]*(?:#m-(\d+))?"
)


def parse_link(ref: str) -> tuple[str, int | None] | None:
    """(session id, line or None) from a chat link the web app copies: https://host/s/<id>#m-<line>."""
    match = LINK.search(ref)
    if match is None:
        return None
    return match.group(1).lower(), int(match.group(2)) if match.group(2) else None


def resolve_session(client: Client, ref: str) -> str:
    """A session id from an id, an id prefix (6+ characters), a chat link
    (https://host/s/<id>#m-<line>), or ``BOT`` / ``BOT:CHAT`` for that bot's main or named chat."""
    if link := parse_link(ref):
        return link[0]
    if len(ref) == 36 and ref.count("-") == 4:
        return ref
    if (
        ":" not in ref
        and len(ref) >= 6
        and all(ch in "0123456789abcdef-" for ch in ref)
    ):
        matches = [s["id"] for s in client.get("/sessions") if s["id"].startswith(ref)]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            sys.exit(f"{ref!r} matches {len(matches)} sessions; give more of the id")
    bot, _, chat = ref.partition(":")
    return client.post(f"/bots/{bot}/chats/{chat or 'main'}")["id"]


# -- commands --------------------------------------------------------------------


def cmd_login(args) -> int:
    key = args.key or os.environ.get("ERGONAUT_API_KEY") or ""
    if not key:
        import getpass

        key = getpass.getpass("API key (ergo_...): ").strip()
    url = args.url.rstrip("/")
    me = Client(url, key).get("/auth/me")
    config = load_config()
    name = args.name or urllib.parse.urlparse(url).hostname or "default"
    config.setdefault("servers", {})[name] = {"url": url, "key": key}
    if not config.get("default") or args.default:
        config["default"] = name
    save_config(config)
    echo(
        f"Saved {name} ({url}) as {me['email'] or me['username']}."
        + (" Default." if config["default"] == name else "")
    )
    return 0


def cmd_servers(args) -> int:
    config = load_config()
    for name, found in config.get("servers", {}).items():
        echo(f"{'*' if name == config.get('default') else ' '} {name}  {found['url']}")
    if os.environ.get("ERGONAUT_URL"):
        echo(f"  (ERGONAUT_URL={os.environ['ERGONAUT_URL']} overrides the default)")
    return 0


def cmd_whoami(client, args) -> int:
    me = client.get("/auth/me")
    version = client.get("/version")
    out = {"server": client.url, "user": me, "version": version}
    if args.json:
        echo(json.dumps(out, indent=2))
    else:
        echo(f"{me['email'] or me['username']} on {client.url}")
        echo(f"version: {json.dumps(version)}")
    return 0


def cmd_bots(client, args) -> int:
    bots = client.get("/bots")
    if args.json:
        echo(json.dumps(bots, indent=2))
        return 0
    for bot in bots:
        parent = f" (under {bot['parent']})" if bot.get("parent") else ""
        chats = ", ".join(c["name"] for c in bot.get("chats", []))
        threads = "threads" if bot.get("orchestration") else "no threads"
        echo(
            f"{bot['name']}{parent}: {bot['description']}  [{threads}; chats: {chats}]"
        )
    return 0


def cmd_bot(client, args) -> int:
    bot = client.get(f"/bots/{args.name}")
    if args.json:
        echo(json.dumps(bot, indent=2))
        return 0
    echo(f"{bot['name']}: {bot['description']}")
    echo(f"engine: {bot['engine']} {bot['model']}   folder: {bot['folder']}")
    echo(f"chats: {', '.join(c['name'] for c in bot['chats'])}")
    echo(f"plugins: {', '.join(bot['plugins']) or 'none'}")
    echo("skills:")
    for skill in bot["skills"]:
        tools = ", ".join(t["name"] for t in skill["tools"])
        always = (
            f" (always in {', '.join(skill['always_in'])})"
            if skill.get("always_in")
            else ""
        )
        echo(
            f"  {skill['name']}{always}: {skill['description'][:120]}"
            + (f"\n    tools: {tools}" if tools else "")
        )
    for schedule in bot.get("schedules", []):
        echo(
            f"schedule {schedule['name']}: {schedule['cron']} next {schedule.get('next_run')}"
        )
    for table in bot.get("tables", []):
        echo(f"table {table.get('name')}: {table.get('rows', '?')} rows")
    return 0


def cmd_threads(client, args) -> int:
    rows = client.get("/sessions", bot=args.bot, q=args.q, status=args.status)
    if args.bucket:
        rows = [s for s in rows if s.get("bucket") == args.bucket]
    rows = rows[: args.limit]
    if args.json:
        echo(json.dumps(rows, indent=2))
        return 0
    for s in rows:
        flags = " ".join(
            f
            for f in [
                s.get("bucket") or "",
                f"waiting:{s['waiting_for']}" if s.get("waiting_for") else "",
                f"workers:{s['workers_running']}/{s['workers_total']}"
                if s.get("workers_total")
                else "",
                " ".join(
                    f"PR#{p.get('number')}:{p.get('state')}" for p in s.get("prs", [])
                ),
            ]
            if f
        )
        echo(
            f"{s['id']}  {s['bot']:<12} {s['title'][:50]:<50} [{flags}] {s['updated_at'][:16]}"
        )
        if s.get("status_line"):
            echo(f"{'':38}{s['status_line'][:110]}")
    return 0


def block_text(block: dict, full: bool) -> str:
    kind = block.get("type")
    clip = (lambda s: s) if full else (lambda s: s if len(s) <= 400 else s[:400] + "…")
    if kind == "text":
        return clip(block.get("text", ""))
    if kind == "tool_use":
        return f"→ {block.get('name')}({clip(json.dumps(block.get('input'), default=str))})"
    if kind == "tool_result":
        content = block.get("content")
        content = (
            content if isinstance(content, str) else json.dumps(content, default=str)
        )
        return f"← {block.get('name') or 'result'}{' ERROR' if block.get('is_error') else ''}: {clip(content)}"
    if kind == "attachment":
        return f"[file: {block.get('label')}]"
    if kind == "thinking" and full:
        return f"(thinking) {block.get('text', '')}"
    return ""


def cmd_show(client, args) -> int:
    session_id = resolve_session(client, args.session)
    line = (
        args.line
        if args.line is not None
        else (parse_link(args.session) or ("", None))[1]
    )
    detail = client.get(f"/sessions/{session_id}", limit=args.limit, around=line)
    if args.json:
        echo(json.dumps(detail, indent=2))
        return 0
    s = detail["session"]
    echo(
        f"# {s['title']}  ({s['bot']}, {s['role']}, {s.get('bucket') or s['status']})  {s['id']}"
    )
    if s.get("status_line"):
        echo(s["status_line"])
    if detail.get("has_more"):
        echo(
            f"(older messages: ergonaut-remote show {s['id']} --limit {args.limit * 2})"
            if line is None
            else f"(older messages: ergonaut-remote show {s['id']} --line {detail['first_line']})"
        )
    for message in detail["messages"]:
        lines = [t for t in (block_text(b, args.full) for b in message["blocks"]) if t]
        if lines:
            mark = "  <- linked message" if message["line"] == line else ""
            echo(f"\n[{message['line']}] {message['role']}:{mark}")
            echo("\n".join(lines))
    for w in detail.get("workers", []):
        echo(
            f"\nworker {w['id']} {w['title']}: {w['status']} {w.get('progress') or ''}".rstrip()
        )
    for r in detail.get("requests", []):
        echo(f"request {r['direction']} {r['other']}: {r['status']} {r['text'][:80]}")
    for pr in detail.get("prs", []):
        echo(f"PR {pr.get('url')} {pr.get('state')} {pr.get('checks') or ''}".rstrip())
    calls = chat_calls(detail)
    if calls and calls[-1]["status"] == "awaiting_approval":
        echo()
        print_reply(calls[-1], s["id"])
    elif (
        calls
        and calls[-1]["status"] not in ("completed", "in_progress")
        and not calls[-1].get("dismissed")
    ):
        echo(
            f"\nLast turn {calls[-1]['status']}: {calls[-1].get('error_summary')}  (ergonaut-remote resume {s['id']})"
        )
    if detail.get("inbox"):
        echo(f"\n{len(detail['inbox'])} message(s) queued for the running turn")
    return 0


def cmd_new(client, args) -> int:
    body = {"title": args.title, "message": args.message, "model": args.model}
    session = client.post(f"/bots/{args.bot}/threads", body)
    echo(f"Thread {session['id']} ({session['title']})", file=sys.stderr)
    return send_and_maybe_wait(client, session["id"], args.message, args)


def cmd_send(client, args) -> int:
    return send_and_maybe_wait(
        client, resolve_session(client, args.session), args.message, args
    )


def cmd_wait(client, args) -> int:
    session_id = resolve_session(client, args.session)
    detail = client.get(f"/sessions/{session_id}", limit=5)
    calls = chat_calls(detail)
    # Wait for whatever runs now; if nothing does, report the latest reply.
    before = {c["id"]: c["status"] for c in calls}
    if not any(c["status"] == "in_progress" for c in calls) and calls:
        before.pop(calls[-1]["id"])
    call = wait_for_turn(client, session_id, before, args.timeout, quiet=args.json)
    if call is None:
        echo(f"Still working after {args.timeout:.0f}s.", file=sys.stderr)
        return 3
    echo(json.dumps(call, indent=2) if args.json else "", end="")
    if not args.json:
        end_progress()
        print_reply(call, session_id)
    return 0


def cmd_approve(client, args) -> int:
    session_id = resolve_session(client, args.session)
    before = snapshot(client, session_id)
    turn = client.post(f"/sessions/{session_id}/approvals", {"approve": not args.deny})
    echo(
        ("Denied." if args.deny else "Approved.")
        + (" The turn continues." if turn.get("queued") else "")
    )
    if args.wait:
        call = wait_for_turn(client, session_id, before, args.timeout)
        end_progress()
        if call is None:
            echo(f"Still working after {args.timeout:.0f}s.", file=sys.stderr)
            return 3
        print_reply(call, session_id)
    return 0


def simple_post(path_template: str, done: str):
    def run(client, args) -> int:
        session_id = resolve_session(client, args.session)
        out = client.post(path_template.format(session_id))
        echo(json.dumps(out, indent=2) if args.json else done)
        return 0

    return run


def cmd_model(client, args) -> int:
    if args.session:
        session_id = resolve_session(client, args.session)
        client.post(f"/sessions/{session_id}/model", {"model": args.set or ""})
        echo(f"{session_id} now uses {args.set or 'the bot default'}")
        return 0
    models = client.get(f"/bots/{args.bot}/models")
    if args.json:
        echo(json.dumps(models, indent=2))
        return 0
    echo(f"default: {models['default']}")
    for m in models["models"]:
        echo(f"  {m['id']}  {m['label']}{'' if m['available'] else '  (no key)'}")
    return 0


def cmd_worker_log(client, args) -> int:
    session_id = resolve_session(client, args.session)
    log = client.get(f"/sessions/{session_id}/workers/{args.worker}/log")
    if args.json:
        echo(json.dumps(log, indent=2))
        return 0
    echo(
        f"# {log.get('title')} ({log.get('source') or 'log'}, {'live' if log.get('live') else 'as of its last check'})"
    )
    for entry in log.get("entries") or []:
        echo(f"[{entry.get('kind', '')}] {entry.get('text', '')}")
    if log.get("error"):
        echo(f"(couldn't read it live: {log['error']})")
    return 0


def cmd_costs(client, args) -> int:
    echo(json.dumps(client.get("/costs", days=args.days, bot=args.bot), indent=2))
    return 0


def cmd_api(client, args) -> int:
    body = json.loads(args.body) if args.body else None
    path = args.path if args.path.startswith("/") else f"/{args.path}"
    echo(json.dumps(client.call(args.method.upper(), path, body), indent=2))
    return 0


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="ergonaut-remote",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "--server", default="", help="a saved server by name (default: the default one)"
    )
    p.add_argument("--json", action="store_true", help="print the API's JSON")
    sub = p.add_subparsers(dest="command", required=True)

    def add(name, func, help_text, needs_client=True):
        cmd = sub.add_parser(name, help=help_text)
        cmd.set_defaults(func=func, needs_client=needs_client)
        return cmd

    def waits(cmd, default_wait=False):
        cmd.add_argument(
            "--wait",
            action="store_true",
            default=default_wait,
            help="wait for the reply and print it",
        )
        cmd.add_argument(
            "--timeout", type=float, default=900, help="seconds to wait (default 900)"
        )

    c = add("login", cmd_login, "save a server and its API key", needs_client=False)
    c.add_argument("url")
    c.add_argument("--name", default="")
    c.add_argument(
        "--key", default="", help="the key (else ERGONAUT_API_KEY, else asked)"
    )
    c.add_argument("--default", action="store_true", help="make it the default server")
    add("servers", cmd_servers, "saved servers", needs_client=False)
    add("whoami", cmd_whoami, "who the key acts as, and the server's version")
    add("bots", cmd_bots, "bots you can use")
    c = add("bot", cmd_bot, "a bot's chats, skills, tools, schedules and tables")
    c.add_argument("name")
    c = add("threads", cmd_threads, "chats and threads, newest first")
    c.add_argument("--bot", default="")
    c.add_argument("--q", default="", help="search message text and titles")
    c.add_argument("--status", default="", help="active or completed")
    c.add_argument(
        "--bucket", default="", help="waiting, working, review, idle or resolved"
    )
    c.add_argument("--limit", type=int, default=30)
    c = add(
        "show",
        cmd_show,
        "a chat's transcript, workers, requests, PRs and pending approvals",
    )
    c.add_argument(
        "session",
        help="session id (or prefix), a chat link (https://host/s/<id>#m-<line>), or BOT / BOT:CHAT",
    )
    c.add_argument("--limit", type=int, default=30, help="messages (default 30)")
    c.add_argument(
        "--line",
        type=int,
        default=None,
        help="show the messages around this line (a link's #m-<line> does the same)",
    )
    c.add_argument(
        "--full", action="store_true", help="don't clip long blocks; include thinking"
    )
    c = add("new", cmd_new, "start a thread with a bot and send the first message")
    c.add_argument("bot")
    c.add_argument("message")
    c.add_argument("--title", default="")
    c.add_argument("--model", default="", help="provider/model from providers.yaml")
    c.add_argument("--interrupt", action="store_true", help=argparse.SUPPRESS)
    waits(c)
    c = add("send", cmd_send, "send a message to a chat (steers a running turn)")
    c.add_argument("session", help="session id (or prefix), or BOT / BOT:CHAT")
    c.add_argument("message")
    c.add_argument(
        "--interrupt", action="store_true", help="stop the running turn first"
    )
    waits(c)
    c = add("wait", cmd_wait, "wait for the running turn to finish and print the reply")
    c.add_argument("session")
    c.add_argument("--timeout", type=float, default=900)
    c = add(
        "approve",
        cmd_approve,
        "approve (or --deny) the tool calls a turn is waiting on",
    )
    c.add_argument("session")
    c.add_argument("--deny", action="store_true")
    waits(c)
    for name, path, done, text in [
        ("stop", "/sessions/{}/stop", "Stopping.", "stop the running turn"),
        (
            "resume",
            "/sessions/{}/resume",
            "Resumed.",
            "retry a chat whose last turn failed",
        ),
        ("close", "/sessions/{}/close", "Archived.", "archive (resolve) a thread"),
    ]:
        c = add(name, simple_post(path, done), text)
        c.add_argument("session")
    c = add("models", cmd_model, "models a bot's chats can use, or set a chat's model")
    c.add_argument("bot", nargs="?", default="")
    c.add_argument("--session", default="", help="set this chat's model")
    c.add_argument("--set", default="", help="provider/model ('' = the bot's default)")
    c = add("worker-log", cmd_worker_log, "a worker's recent output")
    c.add_argument("session")
    c.add_argument("worker")
    c = add("costs", cmd_costs, "usage and spend")
    c.add_argument("--days", type=int, default=7)
    c.add_argument("--bot", default="")
    c = add(
        "api",
        cmd_api,
        "call any API endpoint, e.g. ergonaut-remote api GET /bots/devbox/tree",
    )
    c.add_argument("method")
    c.add_argument("path")
    c.add_argument("body", nargs="?", default="", help="JSON body")
    return p


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    # --json and --server work anywhere on the line, not only before the command.
    flags = []
    for flag in ("--json",):
        while flag in argv:
            argv.remove(flag)
            flags.append(flag)
    if "--server" in argv[1:]:
        at = argv.index("--server")
        flags += argv[at : at + 2]
        del argv[at : at + 2]
    args = parser().parse_args(flags + argv)
    try:
        if not args.needs_client:
            return args.func(args)
        url, key = server(args.server, link=getattr(args, "session", "") or "")
        return args.func(Client(url, key), args)
    except ApiError as e:
        echo(f"ergo: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
