"""Claude engine on a Claude subscription, through the Claude Code CLI.

The engine behaves like ``ClaudeAPIEngine`` and stores the same messages,
but each model call runs the official ``claude`` CLI that is installed and
logged in on this machine (``claude auth login``, or a ``claude setup-token``
token in ``CLAUDE_CODE_OAUTH_TOKEN``). Ergo never reads or handles the login.

One model call is one ``claude -p`` process:

- The history goes in as stream-json frames. Earlier user frames carry
  ``shouldQuery: false``, so the CLI only records them; the last one queries.
- The CLI's own tools, skills and settings are off. Ergo's tools are listed
  through a small MCP server (``claude_code_mcp``). The CLI runs in
  ``dontAsk`` mode, so it denies the tool calls and Ergo runs them itself,
  the same as with the API engine.
- The response is read from the raw stream events of that one request. The
  process is stopped at ``message_stop``, before the CLI could make a
  second request.
- The CLI keeps each call's transcript, and the next call that continues the
  same history resumes it (``--resume`` with ``--resume-session-at`` the
  last reply) and sends only the new messages. Replaying the whole history
  instead would move the context the CLI adds to each request (environment,
  date, reminders), so nothing past the system prompt would come from the
  prompt cache and every request would write the whole chat to it again.
  The CLI's own "permission denied" results for Ergo's tool calls are cut
  from the transcript so the real results take their place. A history that
  no longer matches (compaction, trimmed tool results, a new system prompt
  or tool list) starts a new CLI session with the full replay.

Usage counts against the subscription the way ``claude -p`` and the Agent
SDK do. Anthropic allows this for a person's own login on the unmodified
CLI; it doesn't allow serving other people's requests on that login. Each
person who runs a bot this way logs in with their own account.

The same approach as the Hermes ``claude-subscription-directsdk`` plugin,
without its loopback relay. Checked against Claude Code 2.1.289.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import re
import shutil
import signal
import sys
import tempfile
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

from django_ergo.conversation.engines.claude_api import ClaudeAPIEngine
from django_ergo.conversation.inference_policy import PolicyError
from django_ergo.protected_env import is_protected_env
from django_ergo.protected_env import scrubbed_env

logger = logging.getLogger(__name__)

MCP_SERVER = "ergo"
TOOL_PREFIX = f"mcp__{MCP_SERVER}__"
MAX_TOOL_NAME = 64 - len(TOOL_PREFIX)

# The child keeps the parent's environment minus anything that would pick
# another login or backend (an API key, a gateway, Bedrock, a parent Claude
# Code session); these two are how the CLI finds the subscription login.
KEEP_ENV = {"CLAUDE_CONFIG_DIR", "CLAUDE_CODE_OAUTH_TOKEN"}
DROP_PREFIXES = ("ANTHROPIC_", "CLAUDE")
CHILD_ENV = {
    "ENABLE_TOOL_SEARCH": "false",  # list every tool up front
    "CLAUDE_CODE_MAX_MCP_DESCRIPTION_LENGTH": "100000",
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
    "DISABLE_AUTO_COMPACT": "1",  # Ergo compacts
    "DISABLE_COMPACT": "1",
    "CLAUDE_CODE_TOTAL_TOKENS_REMINDER": "off",
}


# How long a CLI transcript and its resume entries are kept. The prompt cache
# lasts an hour; a day lets a quiet chat still pick its session back up.
RESUME_SECONDS = 24 * 3600
TURN_CONTEXT = re.compile(r"\A<turn-context>\n.*?\n</turn-context>(?:\n\n)?", re.S)


class ClaudeCodeError(RuntimeError):
    """The CLI is missing, logged out, or failed the request."""

    # True once the model started answering (so retrying would bill twice).
    answered = False


def claude_command(command: str = "") -> str:
    return command or os.environ.get("ERGO_CLAUDE_COMMAND") or "claude"


def claude_installed(command: str = "") -> bool:
    return shutil.which(claude_command(command)) is not None


def cli_blocks(message: dict) -> list[dict]:
    """A message's content blocks as the CLI takes them back.

    Tool calls get the MCP name the CLI knows them by. Thinking is dropped:
    stored thinking has no signature, so the API wouldn't take it back.
    """
    content = message.get("content")
    if isinstance(content, str):
        content = [{"type": "text", "text": content}]
    blocks = []
    for block in content or []:
        kind = block.get("type")
        if kind == "tool_use":
            blocks.append({**block, "name": TOOL_PREFIX + block["name"]})
        elif kind != "thinking" and (kind != "text" or block.get("text")):
            blocks.append(block)
    return blocks


def cli_frames(messages: list[dict]) -> list[dict]:
    """Anthropic messages as stream-json input frames; only the last one queries."""
    frames: list[dict] = []
    for message in messages:
        role = message["role"]
        blocks = cli_blocks(message)
        if not blocks:
            continue
        if frames and frames[-1]["type"] == role:
            frames[-1]["message"]["content"].extend(blocks)
        else:
            frames.append({"type": role, "message": {"role": role, "content": blocks}})
    if not frames or frames[-1]["type"] != "user":
        msg = "The Claude Code engine needs history that ends with a user message"
        raise ClaudeCodeError(msg)
    for frame in frames[:-1]:
        if frame["type"] == "user":
            frame["shouldQuery"] = False
    return frames


def mcp_tools(tools: list[dict]) -> list[dict]:
    """Anthropic tool definitions as MCP tools."""
    listed = []
    for tool in tools:
        name = tool["name"]
        if len(name) > MAX_TOOL_NAME:
            msg = f"Tool name {name!r} is longer than {MAX_TOOL_NAME} characters"
            raise ClaudeCodeError(msg)
        listed.append(
            {
                "name": name,
                "description": tool.get("description", ""),
                "inputSchema": tool.get("input_schema") or {"type": "object"},
            }
        )
    return listed


def child_env(overrides: dict | None = None) -> dict:
    """This process's environment minus API-billing and protected variables.

    An override naming a protected variable raises PolicyError. This is
    defense-in-depth, not a sandbox: the CLI runs as the same OS user."""
    if bad := sorted(k for k in overrides or {} if is_protected_env(k)):
        msg = f"Claude Code can't be given {', '.join(bad)}: reserved for private inference"
        raise PolicyError(msg)
    env = {
        key: value
        for key, value in scrubbed_env().items()
        if key in KEEP_ENV or not key.startswith(DROP_PREFIXES)
    }
    env.update(CHILD_ENV)
    env.update(overrides or {})
    return scrubbed_env(env)


def workdir() -> str:
    """A fixed, private working directory: the CLI puts its cwd in the
    prompt, so a new one per call would break prompt caching."""
    path = Path(tempfile.gettempdir()) / f"ergo-claude-code-{os.getuid()}"
    path.mkdir(mode=0o700, exist_ok=True)
    return str(path)


def resume_dir() -> Path:
    """Where resume entries live: one small file per resumable history."""
    path = Path(f"{workdir()}-resume")
    path.mkdir(mode=0o700, exist_ok=True)
    return path


def _block_key(block: dict) -> dict:
    block = {k: v for k, v in block.items() if k != "cache_control"}
    if block.get("type") == "tool_result":
        block["is_error"] = bool(block.get("is_error"))
    return block


def _without_turn_context(message: dict) -> dict:
    """A user message without the ``<turn-context>`` Ergo puts on a turn's first
    message: the next turn sends that message without it, while the CLI's
    transcript keeps it where it was."""
    content = message.get("content")
    if isinstance(content, str):
        return {**message, "content": TURN_CONTEXT.sub("", content, count=1)}
    if content and content[0].get("type") == "text":
        text = TURN_CONTEXT.sub("", content[0].get("text") or "", count=1)
        rest = [{**content[0], "text": text}] if text else []
        return {**message, "content": rest + list(content[1:])}
    return message


def history_keys(
    messages: list[dict], fingerprint: str, *, strip_context: bool = False
) -> list[str]:
    """``keys[k]`` identifies ``messages[:k]`` as the CLI sees them, under
    ``fingerprint`` (model, system prompt, tools, login)."""
    digest = hashlib.sha256(fingerprint.encode())
    keys = [digest.hexdigest()]
    for message in messages:
        seen = message
        if strip_context and message["role"] == "user":
            seen = _without_turn_context(message)
        blocks = [_block_key(block) for block in cli_blocks(seen)]
        digest.update(json.dumps([seen["role"], blocks], sort_keys=True).encode())
        keys.append(digest.copy().hexdigest())
    return keys


def _transcript_path(env: dict, session_id: str) -> Path | None:
    home = Path(env.get("HOME") or Path.home())
    root = Path(env.get("CLAUDE_CONFIG_DIR") or home / ".claude") / "projects"
    return next(root.glob(f"*/{session_id}.jsonl"), None)


def _results_for(row: dict) -> set[str]:
    """The tool calls a transcript row carries results for."""
    content = (row.get("message") or {}).get("content")
    if row.get("type") != "user" or not isinstance(content, list):
        return set()
    return {
        block.get("tool_use_id")
        for block in content
        if isinstance(block, dict) and block.get("type") == "tool_result"
    }


def _cut_denials(path: Path, replies: list[str], tool_ids: set[str]) -> bool:
    """Drop the CLI's "permission denied" results for this reply's tool calls
    from its transcript, so resuming at the reply sends Ergo's results instead.
    False when the transcript still has a result for one of them."""
    rows = [json.loads(line) for line in path.read_text().splitlines() if line]
    replies = set(replies)
    cut = {
        row["uuid"]: row.get("parentUuid")
        for row in rows
        if row.get("type") == "user"
        and row.get("toolDenialKind")
        and row.get("sourceToolAssistantUUID") in replies
    }

    def parent(uuid_: str | None) -> str | None:
        while uuid_ in cut:
            uuid_ = cut[uuid_]
        return uuid_

    kept = [
        {**row, "parentUuid": parent(row.get("parentUuid"))}
        if row.get("parentUuid") in cut
        else row
        for row in rows
        if row.get("uuid") not in cut
    ]
    first = next((i for i, row in enumerate(kept) if row.get("uuid") in replies), None)
    if first is None or any(_results_for(row) & tool_ids for row in kept[first:]):
        return False
    if cut:
        partial = path.with_suffix(".tmp")
        partial.write_text("".join(json.dumps(row) + "\n" for row in kept))
        partial.replace(path)
    return True


_pruned_at = 0.0


def _prune(env: dict) -> None:
    """Forget resume entries and transcripts older than ``RESUME_SECONDS``
    (at most every ten minutes)."""
    global _pruned_at  # noqa: PLW0603
    now = time.time()
    if now - _pruned_at < 600:  # noqa: PLR2004
        return
    _pruned_at = now
    cutoff = now - RESUME_SECONDS
    folders = [resume_dir()]
    home = Path(env.get("HOME") or Path.home())
    projects = Path(env.get("CLAUDE_CONFIG_DIR") or home / ".claude") / "projects"
    # The CLI names a project folder after its cwd, which is only ever ours.
    folders.append(projects / re.sub(r"[^A-Za-z0-9]", "-", workdir()))
    for folder in folders:
        for path in folder.glob("*"):
            with contextlib.suppress(OSError):
                if path.is_file() and path.stat().st_mtime < cutoff:
                    path.unlink()


class _Stream:
    """Builds one Anthropic message from the CLI's raw stream events."""

    def __init__(self):
        self.stopped = False
        self.blocks: dict[int, dict] = {}
        self.partial_json: dict[int, str] = {}
        self.usage: dict = {}
        self.stop_reason = None
        self.stop_details = None
        self.model = ""
        # The CLI's transcript ids for the blocks of this reply.
        self.replies: list[str] = []

    def feed(self, event: dict) -> None:
        kind = event.get("type")
        if kind == "message_start":
            message = event.get("message") or {}
            self.model = message.get("model") or ""
            self.usage.update(message.get("usage") or {})
        elif kind == "content_block_start":
            self.blocks[event["index"]] = dict(event["content_block"])
        elif kind == "content_block_delta":
            self._delta(event["index"], event["delta"])
        elif kind == "content_block_stop":
            index = event["index"]
            if index in self.partial_json:
                self.blocks[index]["input"] = json.loads(
                    self.partial_json.pop(index) or "{}"
                )
        elif kind == "message_delta":
            delta = event.get("delta") or {}
            self.stop_reason = delta.get("stop_reason")
            self.stop_details = delta.get("stop_details")
            self.usage.update(
                {k: v for k, v in (event.get("usage") or {}).items() if v is not None}
            )
        elif kind == "message_stop":
            self.stopped = True

    def _delta(self, index: int, delta: dict) -> None:
        block = self.blocks[index]
        if delta["type"] == "text_delta":
            block["text"] = block.get("text", "") + delta["text"]
        elif delta["type"] == "thinking_delta":
            block["thinking"] = block.get("thinking", "") + delta["thinking"]
        elif delta["type"] == "input_json_delta":
            self.partial_json[index] = (
                self.partial_json.get(index, "") + delta["partial_json"]
            )

    def response(self, model: str) -> SimpleNamespace:
        content = []
        for _, block in sorted(self.blocks.items()):
            kind = block.get("type")
            if kind == "text":
                content.append(SimpleNamespace(type="text", text=block.get("text", "")))
            elif kind == "thinking":
                content.append(
                    SimpleNamespace(type="thinking", thinking=block.get("thinking", ""))
                )
            elif kind == "tool_use":
                name = block["name"].removeprefix(TOOL_PREFIX)
                content.append(
                    SimpleNamespace(
                        type="tool_use",
                        id=block["id"],
                        name=name,
                        input=block.get("input") or {},
                    )
                )
        usage = self.usage
        return SimpleNamespace(
            content=content,
            replies=self.replies,
            stop_reason=self.stop_reason,
            stop_details=self.stop_details,
            model=self.model or model,
            usage=SimpleNamespace(
                input_tokens=usage.get("input_tokens") or 0,
                output_tokens=usage.get("output_tokens") or 0,
                cache_creation_input_tokens=usage.get("cache_creation_input_tokens")
                or 0,
                cache_read_input_tokens=usage.get("cache_read_input_tokens") or 0,
            ),
        )


class ClaudeCodeClient:
    """Stands in for ``anthropic.AsyncAnthropic``: ``messages.create`` runs the CLI."""

    def __init__(
        self,
        command: str = "",
        config_dir: str = "",
        timeout: float = 300,
        effort: str = "",
        env: dict | None = None,
    ):
        self.command = claude_command(command)
        self.config_dir = config_dir
        self.timeout = timeout
        self.effort = effort
        self.env = env or {}
        self.messages = self
        # Called with each request's rate_limit_info (conversation.usage_windows stores it).
        self.on_rate_limit = None
        self._rate_limit: dict | None = None

    async def create(  # noqa: PLR0913
        self,
        *,
        model: str,
        messages: list[dict],
        max_tokens: int | None = None,
        system: str = "",
        tools: list[dict] | None = None,
        tool_choice: dict | None = None,
        **_ignored,
    ) -> SimpleNamespace:
        if shutil.which(self.command) is None:
            msg = (
                f"Claude Code ({self.command!r}) isn't installed: "
                "npm install -g @anthropic-ai/claude-code, then claude auth login"
            )
            raise ClaudeCodeError(msg)
        if tool_choice and tool_choice.get("type") == "tool":
            # The CLI can't force a tool; ask for it instead.
            system = f"{system}\n\nAnswer by calling the {tool_choice['name']} tool."
        cli_frames(messages)  # check the history before starting anything
        with tempfile.TemporaryDirectory(prefix="ergo-claude-code-") as tmp:
            root = Path(tmp)
            (root / "system.md").write_text(system or "", encoding="utf-8")
            (root / "tools.json").write_text(
                json.dumps(mcp_tools(tools or [])), encoding="utf-8"
            )
            server = str(Path(__file__).with_name("claude_code_mcp.py"))
            mcp = {
                "mcpServers": {
                    MCP_SERVER: {
                        "command": sys.executable,
                        "args": [server, str(root / "tools.json")],
                    }
                }
            }
            args = [
                *("-p", "--model", model),
                *("--input-format", "stream-json"),
                *("--output-format", "stream-json"),
                "--verbose",
                "--include-partial-messages",
                *("--tools", ""),
                *("--system-prompt-file", str(root / "system.md")),
                *("--setting-sources", ""),
                "--disable-slash-commands",
                "--strict-mcp-config",
                *("--mcp-config", json.dumps(mcp)),
                *("--permission-mode", "dontAsk"),
                *("--max-turns", "1"),
            ]
            if self.effort:
                args += ["--effort", self.effort]
            env = {}
            if self.config_dir:
                env["CLAUDE_CONFIG_DIR"] = self.config_dir
            if max_tokens:
                env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] = str(max_tokens)
            env.update(self.env)
            env = child_env(env)
            fingerprint = json.dumps(
                [model, system, mcp_tools(tools or []), self.effort, self.command]
                + [env.get("CLAUDE_CONFIG_DIR"), env.get("HOME")]
            )
            resume = self._resumable(messages, fingerprint, env)
            if resume is not None:
                session_id, at, start = resume
                try:
                    response = await self._run(
                        [*args, "--resume", session_id, "--resume-session-at", at],
                        cli_frames(messages[start:]),
                        env,
                        model,
                    )
                except ClaudeCodeError as error:
                    if error.answered:
                        raise
                    logger.warning(
                        "Claude Code couldn't resume %s: %s", session_id, error
                    )
                else:
                    self._remember(messages, response, fingerprint, env, session_id)
                    return response
            session_id = str(uuid.uuid4())
            response = await self._run(
                [*args, "--session-id", session_id], cli_frames(messages), env, model
            )
            self._remember(messages, response, fingerprint, env, session_id)
            return response

    def _resumable(
        self, messages: list[dict], fingerprint: str, env: dict
    ) -> tuple[str, str, int] | None:
        """(CLI session, reply to resume at, first new message) for the longest
        start of ``messages`` an earlier call left a transcript for."""
        keys = history_keys(messages, fingerprint)
        folder = resume_dir()
        for start in range(len(messages) - 1, 0, -1):
            if messages[start - 1]["role"] != "assistant":
                continue
            try:
                entry = json.loads((folder / f"{keys[start]}.json").read_text())
            except (OSError, ValueError):
                continue
            if _transcript_path(env, entry["session"]) is not None:
                return entry["session"], entry["at"], start
        return None

    def _remember(
        self,
        messages: list[dict],
        response: SimpleNamespace,
        fingerprint: str,
        env: dict,
        session_id: str,
    ) -> None:
        """Note where the CLI's transcript of this history ends, so the next
        call that continues it can resume there."""
        if not response.replies:
            return
        reply = {"role": "assistant", "content": []}
        for block in response.content:
            if block.type == "text":
                reply["content"].append({"type": "text", "text": block.text})
            elif block.type == "tool_use":
                reply["content"].append(
                    {
                        "type": "tool_use",
                        "id": block.id,
                        "name": block.name,
                        "input": block.input,
                    }
                )
        tool_ids = {b["id"] for b in reply["content"] if b["type"] == "tool_use"}
        try:
            path = _transcript_path(env, session_id)
            if path is None or not _cut_denials(path, response.replies, tool_ids):
                logger.warning("Claude Code session %s can't be resumed", session_id)
                return
            entry = json.dumps({"session": session_id, "at": response.replies[-1]})
            folder = resume_dir()
            history = [*messages, reply]
            for strip in (False, True):
                key = history_keys(history, fingerprint, strip_context=strip)[-1]
                (folder / f"{key}.json").write_text(entry)
            _prune(env)
        except (OSError, ValueError, KeyError):
            logger.exception("Couldn't record Claude Code session %s", session_id)

    async def _run(
        self, args: list[str], frames: list[dict], env: dict, model: str
    ) -> SimpleNamespace:
        process = await asyncio.create_subprocess_exec(
            self.command,
            *args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=workdir(),
            env=env,
            start_new_session=True,  # so stopping it stops its children too
            limit=64 * 1024 * 1024,
        )
        stderr = asyncio.create_task(process.stderr.read())
        stream = _Stream()
        errors: list[str] = []
        self._rate_limit = None
        try:
            for frame in frames:
                try:
                    process.stdin.write((json.dumps(frame) + "\n").encode())
                    await process.stdin.drain()
                except (BrokenPipeError, ConnectionResetError):
                    msg = "Claude Code exited early"
                    raise ClaudeCodeError(
                        await self._detail(msg, process, errors, stderr)
                    ) from None
                if frame.get("shouldQuery") is False:
                    result = await self._next_result(process, errors)
                    if result is None or result.get("num_turns") != 0:
                        msg = "Claude Code didn't accept the replayed history"
                        raise ClaudeCodeError(
                            await self._detail(msg, process, errors, stderr)
                        )
            process.stdin.close()
            try:
                await self._read_reply(process, stream, errors, stderr)
            except ClaudeCodeError as error:
                error.answered = bool(stream.blocks)
                raise
            return stream.response(model)
        finally:
            await self._stop(process)
            stderr.cancel()
            if self._rate_limit and self.on_rate_limit:
                await self.on_rate_limit(self._rate_limit)

    async def _read_reply(
        self, process, stream: _Stream, errors: list[str], stderr: asyncio.Task
    ) -> None:
        while not stream.stopped:
            event = await self._next_event(process)
            if event is None:
                break
            self._note(event, stream, errors)
        if not stream.stopped:
            msg = "Claude Code request failed"
            raise ClaudeCodeError(await self._detail(msg, process, errors, stderr))

    async def _next_event(self, process) -> dict | None:
        try:
            line = await asyncio.wait_for(process.stdout.readline(), self.timeout)
        except TimeoutError:
            msg = f"Claude Code sent nothing for {self.timeout:g}s"
            raise ClaudeCodeError(msg) from None
        if not line:
            return None
        try:
            return json.loads(line)
        except json.JSONDecodeError:
            msg = (
                f"Claude Code printed something that isn't stream-json: {line[:300]!r}"
            )
            raise ClaudeCodeError(msg) from None

    async def _next_result(self, process, errors: list[str]) -> dict | None:
        while (event := await self._next_event(process)) is not None:
            if event.get("type") == "result":
                return event
            self._note(event, _Stream(), errors)
        return None

    def _note(self, event: dict, stream: _Stream, errors: list[str]) -> None:
        kind = event.get("type")
        if kind == "rate_limit_event":
            self._rate_limit = event.get("rate_limit_info") or None
        elif kind == "stream_event":
            stream.feed(event.get("event") or {})
        elif kind == "assistant" and event.get("uuid") and not event.get("error"):
            # The CLI records each block of the reply under its own id.
            stream.replies.append(event["uuid"])
        elif kind == "assistant" and event.get("error"):
            text = " ".join(
                block.get("text", "")
                for block in (event.get("message") or {}).get("content") or []
                if block.get("type") == "text"
            )
            errors.append(f"{event['error']}: {text}".strip())
        elif kind == "result" and event.get("is_error"):
            errors.append(str(event.get("result") or event.get("subtype") or ""))

    async def _detail(
        self, message: str, process, errors: list[str], stderr: asyncio.Task
    ) -> str:
        detail = [e for e in errors if e]
        await self._stop(process)
        with contextlib.suppress(TimeoutError):
            tail = await asyncio.wait_for(stderr, 2)
            if tail := tail.decode(errors="replace").strip()[-500:]:
                detail.append(tail)
        return f"{message}: {'; '.join(detail)}" if detail else message

    @staticmethod
    async def _stop(process) -> None:
        if process.returncode is not None:
            return
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGTERM)
        try:
            await asyncio.wait_for(process.wait(), 5)
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            await process.wait()


class ClaudeCodeEngine(ClaudeAPIEngine):
    """``ClaudeAPIEngine`` with the Claude Code CLI in place of the API.

    Config: ``model``, ``max_tokens``, and optionally ``command`` (the CLI,
    default ``claude`` or ``$ERGO_CLAUDE_COMMAND``), ``config_dir`` (its
    ``CLAUDE_CONFIG_DIR``), ``effort`` and ``timeout`` (seconds of silence
    before a call fails, default 300).
    """

    transport_type = "cli"

    def __init__(self, config: dict):
        super().__init__(config)
        self.command = config.get("command", "")
        self.config_dir = config.get("config_dir", "")
        self.effort = config.get("effort", "")
        self.timeout = float(config.get("timeout", 300))
        self.provider = config.get("provider", "")

    def _get_client(self):
        if self._client is None:
            self._client = ClaudeCodeClient(
                command=self.command,
                config_dir=self.config_dir,
                timeout=self.timeout,
                effort=self.effort,
            )
            if provider := self.provider:

                async def record(info, provider=provider):
                    from django_ergo.conversation.usage_windows import (
                        arecord_usage_windows,
                    )
                    from django_ergo.conversation.usage_windows import claude_windows

                    await arecord_usage_windows(
                        provider,
                        claude_windows(info),
                        full_snapshot="unifiedWindows" in info,
                    )

                self._client.on_rate_limit = record
        return self._client
