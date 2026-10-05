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
import json
import os
import shutil
import signal
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

from django_ergo.conversation.engines.claude_api import ClaudeAPIEngine

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


class ClaudeCodeError(RuntimeError):
    """The CLI is missing, logged out, or failed the request."""


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
    env = {
        key: value
        for key, value in os.environ.items()
        if key in KEEP_ENV or not key.startswith(DROP_PREFIXES)
    }
    env.update(CHILD_ENV)
    env.update(overrides or {})
    return env


def workdir() -> str:
    """A fixed, private working directory: the CLI puts its cwd in the
    prompt, so a new one per call would break prompt caching."""
    path = Path(tempfile.gettempdir()) / f"ergo-claude-code-{os.getuid()}"
    path.mkdir(mode=0o700, exist_ok=True)
    return str(path)


class _Stream:
    """Builds one Anthropic message from the CLI's raw stream events."""

    def __init__(self):
        self.stopped = False
        self.blocks: dict[int, dict] = {}
        self.partial_json: dict[int, str] = {}
        self.usage: dict = {}
        self.stop_reason = None
        self.model = ""

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
            self.stop_reason = (event.get("delta") or {}).get("stop_reason")
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
            stop_reason=self.stop_reason,
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
        # Called with each request's rate_limit_info (bots.routing reads it).
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
        frames = cli_frames(messages)
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
                "--no-session-persistence",
            ]
            if self.effort:
                args += ["--effort", self.effort]
            env = {}
            if self.config_dir:
                env["CLAUDE_CONFIG_DIR"] = self.config_dir
            if max_tokens:
                env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] = str(max_tokens)
            env.update(self.env)
            return await self._run(args, frames, child_env(env), model)

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
            while not stream.stopped:
                event = await self._next_event(process)
                if event is None:
                    break
                self._note(event, stream, errors)
            if not stream.stopped:
                msg = "Claude Code request failed"
                raise ClaudeCodeError(await self._detail(msg, process, errors, stderr))
            return stream.response(model)
        finally:
            await self._stop(process)
            stderr.cancel()
            if self._rate_limit and self.on_rate_limit:
                await self.on_rate_limit(self._rate_limit)

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
                    from django_ergo.bots.routing import arecord_usage_windows
                    from django_ergo.bots.routing import claude_windows

                    await arecord_usage_windows(provider, claude_windows(info))

                self._client.on_rate_limit = record
        return self._client
