"""OpenAI engine on a ChatGPT subscription, through the Codex CLI.

The engine behaves like ``OpenAIAPIEngine`` and stores the same messages,
but each model call runs ``codex app-server``, the official Codex CLI that
is installed and logged in on this machine (``codex login``). Ergo never
reads or handles the login.

One model call is one ``codex app-server`` process:

- An ephemeral thread starts with the system prompt as its base
  instructions and Ergo's tools as client-run ("dynamic") tools. Codex's own
  tools, skills, plugins and environment notes are off (``CODEX_OFF``), and
  so is each model's agent harness (code mode, sub-agents), through a
  patched copy of the model catalog (``CATALOG_OFF``). One Codex tool stays,
  ``request_user_input``, which Codex documents as Plan mode only.
- The history goes in through ``thread/inject_items`` as Responses API
  items, then an empty ``turn/start`` asks for the next response.
- The response is read from the raw response events of that one request
  (``rawResponseItem/completed`` and ``rawResponse/completed``). The process
  is stopped there, before Codex would run a tool or make a second request,
  and Ergo runs the tool calls itself, the same as with the API engine.

The login must be a ChatGPT account (``require_chatgpt``, on by default), so
these calls never bill an API key. Each response also carries Codex's rate
limit snapshot (``usage_windows``: used percent and reset time of the
5-hour and weekly windows), which a router can use to pick a provider.

Checked against Codex CLI 0.160.0. ``thread/inject_items``, dynamic tools
and raw events are experimental app-server features.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import shutil
import signal
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

from django_ergo.conversation.engines.openai_api import OpenAIAPIEngine

# Config overrides (``-c key=value``) that leave Codex a plain model: no
# shell, files, web, images, skills, plugins, apps or sub-agents, and no
# notes about its sandbox or working directory in the prompt.
CODEX_OFF = {
    "features.shell_tool": "false",
    "features.unified_exec": "false",
    "features.view_image": "false",
    "features.multi_agent": "false",
    "features.goals": "false",
    "features.apps": "false",
    "features.plugins": "false",
    "features.skill_search": "false",
    "features.tool_suggest": "false",
    "features.sleep_tool": "false",
    "features.browser_use": "false",
    "features.computer_use": "false",
    "features.image_generation": "false",
    "features.hooks": "false",
    "features.shell_snapshot": "false",
    "features.memories": "false",
    "web_search": '"disabled"',
    "include_environment_context": "false",
    "include_permissions_instructions": "false",
    "include_apps_instructions": "false",
    "include_collaboration_mode_instructions": "false",
    "skills.include_instructions": "false",
    "skills.bundled.enabled": "false",
    "project_doc_max_bytes": "0",
    "mcp_servers": "{}",
}

# Model catalog fields that turn each model's own agent harness off: tools
# called directly (not from a JavaScript cell), no sub-agents, no patch,
# search or clock tools. The catalog comes from ``codex debug models``.
CATALOG_OFF = {
    "tool_mode": None,
    "multi_agent_version": None,
    "apply_patch_tool_type": None,
    "experimental_supported_tools": [],
    "supports_search_tool": False,
}
CATALOG_TTL = 3600
_catalogs: dict[tuple, tuple[float, str]] = {}

# The child keeps the parent's environment minus anything that would make
# Codex bill an API key instead of the ChatGPT login.
DROP_ENV = {"OPENAI_API_KEY", "OPENAI_BASE_URL", "CODEX_API_KEY"}


class CodexCLIError(RuntimeError):
    """The CLI is missing, logged out, or failed the request."""


def codex_command(command: str = "") -> str:
    return command or os.environ.get("ERGO_CODEX_COMMAND") or "codex"


def codex_installed(command: str = "") -> bool:
    return shutil.which(codex_command(command)) is not None


def _text(content) -> str:
    if isinstance(content, str):
        return content
    return "\n\n".join(
        part.get("text", "")
        for part in content or []
        if part.get("type") in ("text", "input_text", "output_text")
    )


def _input_parts(content) -> list[dict]:
    if isinstance(content, str):
        return [{"type": "input_text", "text": content}] if content else []
    parts = []
    for part in content or []:
        kind = part.get("type")
        if kind == "text" and part.get("text"):
            parts.append({"type": "input_text", "text": part["text"]})
        elif kind == "image_url":
            url = part["image_url"]
            parts.append(
                {"type": "input_image", "image_url": url.get("url", "")}
                if isinstance(url, dict)
                else {"type": "input_image", "image_url": url}
            )
    return parts


def response_items(messages: list[dict]) -> tuple[str, list[dict]]:
    """Chat Completions messages as (instructions, Responses API input items)."""
    system: list[str] = []
    items: list[dict] = []
    for message in messages:
        role = message["role"]
        if role in ("system", "developer"):
            if text := _text(message.get("content")):
                system.append(text)
        elif role == "user":
            if parts := _input_parts(message.get("content")):
                items.append({"type": "message", "role": "user", "content": parts})
        elif role == "assistant":
            if text := _text(message.get("content")):
                items.append(
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": text}],
                    }
                )
            items.extend(
                {
                    "type": "function_call",
                    "call_id": call["id"],
                    "name": call["function"]["name"],
                    "arguments": call["function"].get("arguments") or "{}",
                }
                for call in message.get("tool_calls") or []
            )
        elif role == "tool":
            items.append(
                {
                    "type": "function_call_output",
                    "call_id": message["tool_call_id"],
                    "output": _text(message.get("content")),
                }
            )
    if not items:
        msg = "The Codex engine needs at least one message besides the system prompt"
        raise CodexCLIError(msg)
    return "\n\n".join(system), items


def dynamic_tools(tools: list[dict]) -> list[dict]:
    """Chat Completions tool definitions as app-server dynamic tools."""
    return [
        {
            "type": "function",
            "name": tool["function"]["name"],
            "description": tool["function"].get("description", ""),
            "inputSchema": tool["function"].get("parameters") or {"type": "object"},
        }
        for tool in tools
    ]


def child_env(overrides: dict | None = None) -> dict:
    env = {k: v for k, v in os.environ.items() if k not in DROP_ENV}
    env.update(overrides or {})
    return env


def workdir() -> str:
    """A fixed, private working directory with nothing in it for Codex to read."""
    path = Path(tempfile.gettempdir()) / f"ergo-codex-{os.getuid()}"
    path.mkdir(mode=0o700, exist_ok=True)
    return str(path)


async def model_catalog(command: str, args: list[str], env: dict) -> str:
    """A copy of Codex's model catalog with the agent harness off, as a file.

    Kept for an hour per CLI and login. Returns "" if Codex can't list it,
    in which case calls go ahead with the catalog Codex has.
    """
    key = (command, env.get("CODEX_HOME", ""), tuple(args))
    cached = _catalogs.get(key)
    if cached and time.monotonic() - cached[0] < CATALOG_TTL:
        return cached[1]
    process = await asyncio.create_subprocess_exec(
        command,
        "debug",
        "models",
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        cwd=workdir(),
        env=env,
    )
    try:
        out, _ = await asyncio.wait_for(process.communicate(), 60)
        catalog = json.loads(out)
        for model in catalog["models"]:
            model.update(CATALOG_OFF)
    except (TimeoutError, ValueError, KeyError, TypeError):
        with contextlib.suppress(ProcessLookupError):
            process.kill()
        return ""
    digest = hashlib.sha256(repr(key).encode()).hexdigest()[:16]
    path = Path(workdir()) / f"models-{digest}.json"
    path.write_text(json.dumps(catalog), encoding="utf-8")
    _catalogs[key] = (time.monotonic(), str(path))
    return str(path)


class _ToolCall(SimpleNamespace):
    def model_dump(self) -> dict:
        return {
            "id": self.id,
            "type": "function",
            "function": {
                "name": self.function.name,
                "arguments": self.function.arguments,
            },
        }


class _Response:
    """Collects one response's output items into a ChatCompletion lookalike."""

    def __init__(self):
        self.text: list[str] = []
        self.calls: list[_ToolCall] = []
        self.usage: dict = {}
        self.done = False
        # Injected history comes back as raw items too, under another turn
        # id; only the turn that made the response counts.
        self.items: list[tuple[str, dict]] = []

    def finish(self, turn: str, usage: dict) -> None:
        for item_turn, item in self.items:
            if item_turn == turn:
                self.item(item)
        self.usage = usage
        self.done = True

    def item(self, item: dict) -> None:
        kind = item.get("type")
        if kind == "message" and item.get("role") == "assistant":
            self.text += [
                part.get("text", "")
                for part in item.get("content") or []
                if part.get("type") == "output_text"
            ]
        elif kind == "function_call":
            self.calls.append(
                _ToolCall(
                    id=item["call_id"],
                    type="function",
                    function=SimpleNamespace(
                        name=item["name"], arguments=item.get("arguments") or "{}"
                    ),
                )
            )

    def completion(self, model: str, rate_limits: dict | None) -> SimpleNamespace:
        usage = self.usage
        text = "".join(self.text)
        return SimpleNamespace(
            model=model,
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        role="assistant",
                        content=text or None,
                        tool_calls=self.calls or None,
                    ),
                    finish_reason="tool_calls" if self.calls else "stop",
                )
            ],
            usage=SimpleNamespace(
                prompt_tokens=usage.get("inputTokens") or 0,
                completion_tokens=usage.get("outputTokens") or 0,
                prompt_tokens_details={
                    "cached_tokens": usage.get("cachedInputTokens") or 0,
                    "cache_write_tokens": usage.get("cacheWriteInputTokens") or 0,
                },
                completion_tokens_details={
                    "reasoning_tokens": usage.get("reasoningOutputTokens") or 0
                },
            ),
            usage_windows=rate_limits,
        )


class CodexClient:
    """Stands in for ``openai.AsyncOpenAI``: ``chat.completions.create`` runs Codex."""

    def __init__(  # noqa: PLR0913
        self,
        command: str = "",
        codex_home: str = "",
        timeout: float = 300,
        effort: str = "",
        config: dict | None = None,
        env: dict | None = None,
        require_chatgpt: bool = True,
    ):
        self.command = codex_command(command)
        self.codex_home = codex_home
        self.timeout = timeout
        self.effort = effort
        self.config = {**CODEX_OFF, **(config or {})}
        self.env = env or {}
        self.require_chatgpt = require_chatgpt
        self.chat = SimpleNamespace(completions=self)
        # Called with each response's rate-limit snapshot (bots.routing reads it).
        self.on_rate_limit = None

    async def create(
        self,
        *,
        model: str,
        messages: list[dict],
        tools: list[dict] | None = None,
        tool_choice=None,
        **_ignored,
    ) -> SimpleNamespace:
        if shutil.which(self.command) is None:
            msg = (
                f"Codex ({self.command!r}) isn't installed: "
                "npm install -g @openai/codex, then codex login"
            )
            raise CodexCLIError(msg)
        instructions, items = response_items(messages)
        if isinstance(tool_choice, dict) and tool_choice.get("function"):
            # Codex can't force a tool; ask for it instead.
            name = tool_choice["function"]["name"]
            instructions = f"{instructions}\n\nAnswer by calling the {name} tool."
        overrides = []
        for key, value in self.config.items():
            overrides += ["-c", f"{key}={value}"]
        env = dict(self.env)
        if self.codex_home:
            env["CODEX_HOME"] = self.codex_home
        env = child_env(env)
        if catalog := await model_catalog(self.command, overrides, env):
            overrides += ["-c", f"model_catalog_json={json.dumps(catalog)}"]
        session = _Session(self, ["app-server", *overrides], env)
        try:
            await session.start()
            return await session.call(model, instructions, items, tools or [])
        finally:
            await session.stop()
            if session.rate_limits and self.on_rate_limit:
                await self.on_rate_limit(session.rate_limits)


class _Session:
    """One ``codex app-server`` process speaking JSON-RPC over stdio."""

    def __init__(self, client: CodexClient, args: list[str], env: dict):
        self.client = client
        self.args = args
        self.env = env
        self.process = None
        self.ids = iter(range(1, 1_000_000))
        self.errors: list[str] = []
        self.rate_limits: dict | None = None
        self.response = _Response()
        self.stderr = None

    async def start(self) -> None:
        self.process = await asyncio.create_subprocess_exec(
            self.client.command,
            *self.args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=workdir(),
            env=self.env,
            start_new_session=True,  # so stopping it stops its children too
            limit=64 * 1024 * 1024,
        )
        self.stderr = asyncio.create_task(self.process.stderr.read())
        await self.request(
            "initialize",
            {
                "clientInfo": {"name": "django_ergo", "version": "1"},
                "capabilities": {"experimentalApi": True},
            },
        )
        await self.send({"method": "initialized"})

    async def call(
        self, model: str, instructions: str, items: list[dict], tools: list[dict]
    ) -> SimpleNamespace:
        if self.client.require_chatgpt:
            account = (await self.request("account/read", {})) or {}
            kind = (account.get("account") or {}).get("type")
            if kind != "chatgpt":
                msg = (
                    "Codex isn't logged in with a ChatGPT account "
                    f"({kind or 'not logged in'}): run codex login"
                )
                raise CodexCLIError(msg)
        started = await self.request(
            "thread/start",
            {
                "ephemeral": True,
                "experimentalRawEvents": True,
                "model": model,
                "baseInstructions": instructions or None,
                "dynamicTools": dynamic_tools(tools),
                "approvalPolicy": "never",
                "sandbox": "read-only",
            },
        )
        thread = started["thread"]["id"]
        await self.request("thread/inject_items", {"threadId": thread, "items": items})
        turn: dict = {"threadId": thread, "input": []}
        if self.client.effort:
            turn["effort"] = self.client.effort
        await self.request("turn/start", turn)
        while not self.response.done:
            message = await self.read()
            if message is None:
                msg = "Codex request failed"
                raise CodexCLIError(await self.detail(msg))
            await self.handle(message)
        if not (self.rate_limits or {}).get("primary"):
            # Codex doesn't always announce the windows after a short turn.
            with contextlib.suppress(CodexCLIError, TimeoutError):
                read = await asyncio.wait_for(
                    self.request("account/rateLimits/read", {}), 10
                )
                self.rate_limits = (read or {}).get("rateLimits") or self.rate_limits
        return self.response.completion(model, self.rate_limits)

    async def handle(self, message: dict) -> None:
        method = message.get("method")
        params = message.get("params") or {}
        if "id" in message and method:
            # A server request (a tool call, an approval): Ergo answers tool
            # calls itself, after this process is gone, so decline them all.
            await self.send(
                {
                    "id": message["id"],
                    "error": {"code": -32601, "message": "not handled by Ergo"},
                }
            )
        elif method == "rawResponseItem/completed":
            self.response.items.append((params.get("turnId"), params.get("item") or {}))
        elif method == "rawResponse/completed":
            self.response.finish(params.get("turnId"), params.get("usage") or {})
        elif method == "account/rateLimits/updated":
            self.rate_limits = params.get("rateLimits")
        elif method == "error":
            error = params.get("error") or {}
            self.errors.append(str(error.get("message") or error))
        elif method == "turn/completed":
            error = (params.get("turn") or {}).get("error") or {}
            if text := error.get("message"):
                self.errors.append(str(text))
            msg = "Codex request failed"
            raise CodexCLIError(await self.detail(msg))

    async def send(self, message: dict) -> None:
        try:
            self.process.stdin.write((json.dumps(message) + "\n").encode())
            await self.process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            msg = "Codex exited early"
            raise CodexCLIError(await self.detail(msg)) from None

    async def request(self, method: str, params: dict):
        request_id = next(self.ids)
        await self.send({"id": request_id, "method": method, "params": params})
        while True:
            message = await self.read()
            if message is None:
                msg = f"Codex exited during {method}"
                raise CodexCLIError(await self.detail(msg))
            if message.get("id") == request_id and "method" not in message:
                if error := message.get("error"):
                    msg = f"Codex refused {method}: {error.get('message') or error}"
                    raise CodexCLIError(msg)
                return message.get("result")
            await self.handle(message)

    async def read(self) -> dict | None:
        try:
            line = await asyncio.wait_for(
                self.process.stdout.readline(), self.client.timeout
            )
        except TimeoutError:
            msg = f"Codex sent nothing for {self.client.timeout:g}s"
            raise CodexCLIError(msg) from None
        if not line:
            return None
        try:
            return json.loads(line)
        except json.JSONDecodeError:
            msg = f"Codex printed something that isn't JSON-RPC: {line[:300]!r}"
            raise CodexCLIError(msg) from None

    async def detail(self, message: str) -> str:
        detail = [e for e in self.errors if e]
        await self.stop()
        with contextlib.suppress(TimeoutError):
            tail = await asyncio.wait_for(self.stderr, 2)
            if tail := tail.decode(errors="replace").strip()[-500:]:
                detail.append(tail)
        return f"{message}: {'; '.join(detail)}" if detail else message

    async def stop(self) -> None:
        process = self.process
        if process is None or process.returncode is not None:
            return
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGTERM)
        try:
            await asyncio.wait_for(process.wait(), 5)
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            await process.wait()
        if self.stderr and not self.stderr.done():
            self.stderr.cancel()


class CodexCLIEngine(OpenAIAPIEngine):
    """``OpenAIAPIEngine`` with the Codex CLI in place of the API.

    Config: ``model``, and optionally ``command`` (the CLI, default ``codex``
    or ``$ERGO_CODEX_COMMAND``), ``codex_home`` (its ``CODEX_HOME``),
    ``effort`` (reasoning effort, e.g. ``low``/``medium``/``high``),
    ``timeout`` (seconds of silence before a call fails, default 300),
    ``codex_config`` (extra ``-c`` overrides) and ``require_chatgpt``
    (default true: refuse to run on an API-key login).
    """

    transport_type = "cli"

    def __init__(self, config: dict):
        super().__init__(config)
        self.command = config.get("command", "")
        self.codex_home = config.get("codex_home", "")
        self.effort = config.get("effort") or config.get("reasoning_effort") or ""
        self.timeout = float(config.get("timeout", 300))
        self.codex_config = dict(config.get("codex_config") or {})
        self.require_chatgpt = bool(config.get("require_chatgpt", True))
        self.provider = config.get("provider", "")

    def _get_client(self):
        if self._client is None:
            self._client = CodexClient(
                command=self.command,
                codex_home=self.codex_home,
                timeout=self.timeout,
                effort=self.effort,
                config=self.codex_config,
                require_chatgpt=self.require_chatgpt,
            )
            if provider := self.provider:

                async def record(rate_limits, provider=provider):
                    from django_ergo.bots.routing import arecord_usage_windows
                    from django_ergo.bots.routing import codex_windows

                    await arecord_usage_windows(provider, codex_windows(rate_limits))

                self._client.on_rate_limit = record
        return self._client
