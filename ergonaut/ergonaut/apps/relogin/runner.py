"""The worker side of a re-login: run the store's fixed command in a PTY on the login host
(over ``ssh -T`` and the ``RELAY`` below), show its sign-in link and code, pass back the one submission, then check.

The CLI's output stays in this process's memory (bounded) and is never logged or stored;
only our own words go to logs, the audit row and the main chat."""

from __future__ import annotations

import contextlib
import logging
import os
import select
import shlex
import shutil
import subprocess
import tempfile
import time
from datetime import timedelta
from pathlib import Path

from django.db.models import Q
from django.utils import timezone

from ergonaut.apps.relogin import handoff, parsing, stores
from ergonaut.apps.relogin.models import ReloginSession

logger = logging.getLogger(__name__)

READ_LIMIT = 64 * 1024  # the output kept in memory, at most
URL_WAIT = 45  # seconds for the sign-in link to appear before failing closed
EXIT_WAIT = 60  # seconds for the CLI to finish after the submission or device approval
# PATH on the login host: ssh runs a non-login shell, so put the user's bin dir first.
# No browser to open, no colours, and plain links instead of OSC 8 hyperlinks.
REMOTE_ENV = (
    'PATH="$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin" TERM=xterm-256color'
    " BROWSER=false NO_COLOR=1 FORCE_HYPERLINK=0"
)
CURL = "curl -sS --max-time 10 -o /dev/null -w '%{http_code}' -K -"

CANT_READ = "Could not read the login URL from the CLI."
NO_PROMPT = "The CLI didn't ask for a code where expected, so nothing was sent."
RELAY_FAILED = "The sign-in callback could not be delivered to the login."
WRONG_PORT = "The login is not listening on its usual callback port (is another login running?)."
UNREACHABLE = "Could not reach the login host over ssh."
CRASHED = "The re-login stopped on an internal error."


# Runs on the login host as `python3 -c RELAY <argv>`: gives the CLI its own terminal (2000
# columns, so links don't wrap) and relays bytes between it and ssh's pipes. When ssh's stdin
# closes (cancel, timeout, the worker or the connection going away) it hangs up and then kills
# the CLI's whole process group. ssh itself requests no terminal: devbox's login shell would
# attach a tty session to the shared tmux instead of running the command.
RELAY = r"""
import fcntl, os, pty, select, signal, struct, sys, termios, time
pid, fd = pty.fork()
if pid == 0:
    fcntl.ioctl(0, termios.TIOCSWINSZ, struct.pack("HHHH", 50, 2000, 0, 0))
    os.execvp(sys.argv[1], sys.argv[1:])
def stop(*_):
    for sig in (signal.SIGHUP, signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(pid, sig)
        except OSError:
            return
        for _ in range(10):
            if os.waitpid(pid, os.WNOHANG)[0]:
                return
            time.sleep(0.1)
for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGPIPE):
    signal.signal(sig, lambda *_: (stop(), os._exit(129)))
try:
    while True:
        ready = select.select([0, fd], [], [], 1)[0]
        if fd in ready:
            try:
                data = os.read(fd, 4096)
            except OSError:
                data = b""
            if not data:
                break
            os.write(1, data)
        if 0 in ready:
            data = os.read(0, 4096)
            if not data:
                stop()
                os._exit(129)
            os.write(fd, data)
except Exception:
    stop()
    os._exit(129)
status = os.waitpid(pid, 0)[1]
code = os.waitstatus_to_exitcode(status)
sys.exit(code if code >= 0 else 128 - code)
"""
REMOTE_PYTHON = "/usr/bin/python3"


def remote_command(argv: tuple[str, ...]) -> str:
    """The fixed shell text run on the login host: env, the PTY relay, the allow-listed argv."""
    if tuple(argv) not in stores.COMMANDS.values():
        raise stores.StoreError("Command is not on the allow-list.")
    return f"exec env {REMOTE_ENV} {REMOTE_PYTHON} -c {shlex.quote(RELAY)} {shlex.join(argv)}"


def ssh_argv(target: stores.SshTarget, known_hosts: str, remote: str) -> list[str]:
    return [
        shutil.which("ssh") or "ssh",
        "-F",
        "/dev/null",  # nothing from a config file: this exact route, key and host key
        "-T",  # no remote tty (see RELAY)
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        f"UserKnownHostsFile={known_hosts}",
        "-o",
        "GlobalKnownHostsFile=/dev/null",
        "-o",
        "IdentitiesOnly=yes",
        "-i",
        str(target.identity),
        "-o",
        "ConnectTimeout=10",
        "-o",
        "ServerAliveInterval=15",
        "-o",
        "ServerAliveCountMax=3",
        "-o",
        "ForwardAgent=no",
        "-o",
        "ClearAllForwardings=yes",
        "-o",
        "LogLevel=ERROR",
        "-p",
        str(target.port),
        "-l",
        target.user,
        "--",
        target.host,
        remote,
    ]


@contextlib.contextmanager
def known_hosts(target: stores.SshTarget):
    """A known_hosts file that pins the login host's key (read from ``host_key``)."""
    fields = target.host_key.read_text().split()
    if len(fields) < 2 or not fields[0].startswith(("ssh-", "ecdsa-")):
        raise stores.StoreError("The login host's pinned ssh key can't be read.")
    name = target.host if target.port == 22 else f"[{target.host}]:{target.port}"
    folder = tempfile.mkdtemp(prefix="relogin-")
    path = Path(folder) / "known_hosts"
    try:
        path.write_text(f"{name} {fields[0]} {fields[1]}\n")
        yield str(path)
    finally:
        shutil.rmtree(folder, ignore_errors=True)


def _env() -> dict:
    return {
        "HOME": os.environ.get("HOME", "/tmp"),
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "LANG": "C.UTF-8",
    }


class Remote:
    """The ssh process holding the login, on pipes (the terminal is RELAY's, on the host)."""

    def __init__(self, argv: list[str]):
        self.proc = subprocess.Popen(  # noqa: S603 - fixed argv from the allow-list
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            close_fds=True,
            start_new_session=True,
            env=_env(),
        )

    def read(self, timeout: float) -> bytes | None:
        """Output so far; b"" if none yet, None once it closed."""
        ready, _, _ = select.select([self.proc.stdout], [], [], timeout)
        if not ready:
            return b""
        try:
            data = os.read(self.proc.stdout.fileno(), 4096)
        except OSError:
            return None
        return data or None

    def write(self, data: bytes) -> None:
        self.proc.stdin.write(data)
        self.proc.stdin.flush()

    def alive(self) -> bool:
        return self.proc.poll() is None

    def stop(self) -> int:
        """End the login and return the exit code: Ctrl-C to the CLI, then close stdin (RELAY
        kills the CLI's process group), then TERM and KILL to ssh."""
        if self.alive():
            with contextlib.suppress(OSError, ValueError):
                self.write(b"\x03")
            with contextlib.suppress(subprocess.TimeoutExpired):
                self.proc.wait(timeout=2)
        with contextlib.suppress(OSError, ValueError):
            self.proc.stdin.close()
        with contextlib.suppress(subprocess.TimeoutExpired):
            self.proc.wait(timeout=5)
        if self.alive():
            self.proc.terminate()
            with contextlib.suppress(subprocess.TimeoutExpired):
                self.proc.wait(timeout=3)
        if self.alive():
            self.proc.kill()
        code = self.proc.wait()
        with contextlib.suppress(OSError, ValueError):
            self.proc.stdout.close()
        return code


def relay_redirect(target: stores.SshTarget, known_hosts_file: str, url: str) -> bool:
    """GET the checked callback URL on the login host; the URL goes on curl's stdin."""
    if '"' in url or "\\" in url or "\n" in url:
        return False
    try:
        proc = subprocess.run(  # noqa: S603 - fixed command, the URL is on stdin
            ssh_argv(target, known_hosts_file, CURL),
            input=f'url = "{url}"\n',
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
            env=_env(),
        )
    except (OSError, subprocess.SubprocessError):
        return False
    status = proc.stdout.strip()[-3:]
    return proc.returncode == 0 and status.isdigit() and 200 <= int(status) < 400


def _update(session_id: str, **fields) -> None:
    ReloginSession.objects.filter(pk=session_id, outcome="").update(**fields)


def drive(session: ReloginSession, store: stores.Store, proc: Remote, relay) -> tuple[str | None, str]:
    """Run the login until it exits, is cancelled or times out.

    Returns (outcome, error): outcome None means the CLI exited by itself."""
    output = bytearray()
    shown = None
    submitted_at = None
    started = time.monotonic()
    deadline = session.expires_at
    while True:
        now = timezone.now()
        if handoff.cancelled(session.pk):
            return "cancelled", ""
        if now >= deadline:
            return "expired", ""
        if submitted_at is not None and time.monotonic() - submitted_at > EXIT_WAIT:
            return "failed", "The login didn't finish after the submission."
        chunk = proc.read(0.5)
        if chunk is None:
            return (None, "") if shown else ("failed", CANT_READ)
        if chunk:
            output += chunk
            del output[:-READ_LIMIT]
        text = parsing.clean(bytes(output))
        if shown is None:
            shown = parsing.read_output(text, store.shape, store.url_hosts)
            if shown is None:
                if time.monotonic() - started > URL_WAIT:
                    return "failed", CANT_READ
                continue
            if (
                store.shape == "paste_redirect"
                and parsing.redirect_uri(shown.url) != f"http://localhost:{store.port}/callback"
            ):
                return "failed", WRONG_PORT  # the CLI moved its callback; the paste couldn't reach it
            if shown.seconds:
                deadline = min(deadline, now + timedelta(seconds=shown.seconds))
            handoff.show(
                session.pk,
                {"url": shown.url, "code": shown.code, "state": shown.state, "expires_at": deadline.timestamp()},
                ttl=(deadline - now).total_seconds() + 30,
            )
            _update(session.pk, state="waiting", expires_at=deadline)
            continue
        if store.shape == "device_code" or submitted_at is not None:
            continue
        value = handoff.take_input(session.pk)
        if not value:
            continue
        submitted_at = time.monotonic()
        _update(session.pk, state="submitted")
        if store.shape == "paste_code":
            if not parsing.paste_prompt_seen(text):
                return "failed", NO_PROMPT
            proc.write(value.encode() + b"\r")
        elif not relay(value):
            return "failed", RELAY_FAILED
        value = None


def _post_check(bot, ctx, key: str, before) -> dict:
    hook = stores.hook(bot, "relogin_post_check")
    if hook is None:
        return {}
    try:
        result = hook(ctx, key, before) or {}
    except Exception as exc:  # noqa: BLE001 - recorded in our own words
        logger.warning("relogin post-check raised %s", type(exc).__name__)
        return {"ok": False, "state": "unknown", "detail": "The post-check could not run."}
    detail = parsing._URL.sub("[link]", str(result.get("detail") or ""))[:300]
    account = result.get("account") if result.get("account") in {"same", "different", "unknown"} else ""
    return {
        "ok": bool(result.get("ok")),
        "state": str(result.get("state") or "")[:20],
        "detail": detail,
        "account": account,
    }


def _identity(bot, ctx, key: str):
    hook = stores.hook(bot, "relogin_identity")
    if hook is None:
        return None
    try:
        return hook(ctx, key)
    except Exception as exc:  # noqa: BLE001 - no identity means "unknown" later
        logger.warning("relogin identity check raised %s", type(exc).__name__)
        return None


def finish(session: ReloginSession, outcome: str, *, error: str = "", **fields) -> bool:
    ended = ReloginSession.objects.filter(pk=session.pk, outcome="").update(
        outcome=outcome, state="ended", ended_at=timezone.now(), error=error[:300], **fields
    )
    handoff.clear(session.pk)
    if ended:
        logger.info("relogin %s ended: %s", session.pk, outcome)
    return bool(ended)


def announce(bot, session: ReloginSession) -> None:
    """One event message in the user's main chat; never a link or a code."""
    from asgiref.sync import async_to_sync
    from django_ergo.conversation.identity import system_identity
    from django_ergo.conversation.messages import add_message

    session.refresh_from_db()
    if session.outcome == "ok":
        text = f"Re-login of {session.store} completed."
        if session.account == "different":
            text += " Warning: it signed in to a different account than before."
    else:
        text = f"Re-login of {session.store} {session.outcome}."
        if session.error:
            text += f" {session.error}"
        text += f" Manual fix: run `{session.command}` on devbox."
    try:
        chat = async_to_sync(bot.chat_session)(session.user, "main")
        async_to_sync(add_message)(
            chat,
            "assistant",
            [{"block_type": "text", "text": text}],
            author=system_identity("ergonaut:relogin", "Re-login"),
            provenance={"kind": "event"},
        )
    except Exception as exc:  # noqa: BLE001 - the audit row is the record; the message is a courtesy
        logger.warning("relogin %s: event message failed (%s)", session.pk, type(exc).__name__)


def run(session_id: str, *, spawn=None) -> None:
    """The Celery task body. ``spawn(argv) -> Remote`` is replaceable for tests."""
    from django_ergo.bots.tools import ToolContext

    session = ReloginSession.objects.select_related("user").filter(pk=session_id, outcome="").first()
    if session is None:
        return
    bot = None
    try:
        bot = stores.get_bot(session.bot_name)
        store = stores.load(session.bot_name, session.store)
        target = stores.ssh_target(bot)
        if store.command != session.command:
            raise stores.StoreError("The store's command changed; start again.")
    except stores.StoreError as exc:
        finish(session, "failed", error=str(exc))
        if bot is not None:
            announce(bot, session)
        return
    ctx = ToolContext(bot=bot, user=session.user)
    try:
        before = _identity(bot, ctx, store.key)
        with known_hosts(target) as known_hosts_file:
            argv = ssh_argv(target, known_hosts_file, remote_command(store.argv))
            proc = (spawn or Remote)(argv)
            try:
                outcome, error = drive(session, store, proc, lambda url: relay_redirect(target, known_hosts_file, url))
            finally:
                exit_code = proc.stop()
        handoff.clear(session.pk)
        if outcome == "failed" and error == CANT_READ and exit_code == 255:  # noqa: PLR2004 - ssh's own failure
            error = UNREACHABLE
        if outcome is None:
            outcome, error = ("ok", "") if exit_code == 0 else ("failed", f"The login exited with code {exit_code}.")
        _update(session.pk, state="checking", exit_code=exit_code)
        check = _post_check(bot, ctx, store.key, before)
        if outcome == "ok" and check and not check["ok"]:
            outcome, error = "failed", "The login finished but the store still doesn't check out."
        finish(
            session,
            outcome,
            error=error,
            exit_code=exit_code,
            post_check={k: v for k, v in check.items() if k != "account"},
            account=check.get("account", ""),
        )
    except Exception as exc:  # noqa: BLE001 - never let a traceback carry the CLI output anywhere
        logger.error("relogin %s crashed (%s)", session_id, type(exc).__name__)
        finish(session, "failed", error=CRASHED)
    announce(bot, session)


def sweep() -> int:
    """End sessions whose worker is gone: past their end time by more than a minute."""
    stale = ReloginSession.objects.filter(Q(outcome=""), expires_at__lt=timezone.now() - timedelta(seconds=90))
    count = 0
    for session in stale:
        if finish(session, "expired", error="The re-login worker stopped before the session ended."):
            count += 1
    return count
