"""Use another Ergonaut server's bots: list them, start and follow threads, answer approvals (the ergonaut-remote client)."""

import importlib.util
import io
from pathlib import Path

from django_ergo.bots import bot_tool

_cli = None


def cli():
    """scripts/ergonaut_remote.py, the same client Claude Code and Codex run."""
    global _cli
    if _cli is None:
        spec = importlib.util.spec_from_file_location(
            "ergo_client_cli", Path(__file__).parent / "scripts" / "ergonaut_remote.py"
        )
        _cli = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_cli)
    return _cli


def run(ctx, *argv: str) -> str:
    """Run one ``ergonaut-remote`` command against ERGONAUT_URL with ERGONAUT_API_KEY; its output as text."""
    url, key = ctx.secret("ERGONAUT_URL"), ctx.secret("ERGONAUT_API_KEY")
    if not url or not key:
        raise RuntimeError(
            "Set ERGONAUT_URL and ERGONAUT_API_KEY (an API key from that server) to use the Ergo client."
        )
    module = cli()
    args = module.parser().parse_args(list(argv))
    out = io.StringIO()
    token = module.OUTPUT.set(out)
    try:
        code = args.func(module.Client(url, key), args)
    except module.ApiError as e:
        raise RuntimeError(str(e)) from None
    finally:
        module.OUTPUT.reset(token)
    text = out.getvalue().strip()
    if code == 3:
        text += "\n(Still working. Check again later with ergo_client_show.)"
    return text[-12000:]


@bot_tool(takes_context=True)
def ergo_client_bots(ctx) -> str:
    """The bots on the remote Ergonaut server, with their chats."""
    return run(ctx, "bots")


@bot_tool(takes_context=True)
def ergo_client_threads(
    ctx, bot: str = "", bucket: str = "", query: str = "", limit: int = 20
) -> str:
    """Chats and threads on the remote server, newest first. bucket: waiting, working, review, idle or resolved."""
    return run(
        ctx,
        "threads",
        "--bot",
        bot,
        "--bucket",
        bucket,
        "--q",
        query,
        "--limit",
        str(limit),
    )


@bot_tool(takes_context=True)
def ergo_client_show(ctx, session: str, limit: int = 20) -> str:
    """A remote chat's recent transcript, workers, PRs and pending approvals. session: an id, or BOT / BOT:CHAT."""
    return run(ctx, "show", session, "--limit", str(limit))


@bot_tool(takes_context=True)
def ergo_client_new_thread(
    ctx, bot: str, message: str, title: str = "", wait_seconds: int = 0
) -> str:
    """Start a thread with a remote bot. wait_seconds > 0 waits (up to 300) for its reply; else check later."""
    argv = ["new", bot, message, "--title", title]
    if wait_seconds > 0:
        argv += ["--wait", "--timeout", str(min(wait_seconds, 300))]
    return run(ctx, *argv)


@bot_tool(takes_context=True)
def ergo_client_send(ctx, session: str, message: str, wait_seconds: int = 0) -> str:
    """Send a message to a remote chat (it steers a running turn). wait_seconds > 0 waits (up to 300) for the reply."""
    argv = ["send", session, message]
    if wait_seconds > 0:
        argv += ["--wait", "--timeout", str(min(wait_seconds, 300))]
    return run(ctx, *argv)


@bot_tool(takes_context=True, requires_approval=True)
def ergo_client_approve(ctx, session: str, approve: bool = True) -> str:
    """Approve (or deny) the tool calls a remote chat is waiting on."""
    return run(ctx, "approve", session, *([] if approve else ["--deny"]))
