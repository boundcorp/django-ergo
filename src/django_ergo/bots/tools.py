"""Python tools for bots: the @bot_tool decorator and FunctionToolkit.

A bot's tool module is a plain Python file::

    from django_ergo.bots import bot_tool

    @bot_tool(description="Find a recipe by name")
    def find_recipe(query: str, limit: int = 5) -> list[dict]:
        ...

    @bot_tool(description="Add to the shopping list", requires_approval=True,
              takes_context=True)
    def add_to_list(ctx, item: str) -> str:
        ...   # ctx.user, ctx.session, ctx.bot

Parameters come from the signature (str, int, float, bool, list, dict),
or pass ``parameters=`` as a JSON Schema ``properties`` mapping. A module
may also define ``toolkits(ctx) -> list[Toolkit]``.

``@bot_context`` marks a function whose text goes into the model's context
on every turn, such as live data the bot should always see::

    @bot_context(title="Shopping list")
    def shopping_list(ctx, message: str) -> str:
        return render(tandoor(ctx).shopping_list())

Tools and context functions read per-user credentials with
``ctx.secret("TANDOOR_API_KEY")``: the environment variable
``TANDOOR_API_KEY__<USERNAME>`` if set, else ``TANDOOR_API_KEY``.
"""

from __future__ import annotations

import hashlib
import importlib.util
import inspect
import json
import logging
import os
import re
import sys
from dataclasses import dataclass
from dataclasses import field
from typing import TYPE_CHECKING
from typing import Any
from typing import get_type_hints

from django_ergo.conversation.adapters import OpenAIToolAdapter
from django_ergo.conversation.toolkit import Toolkit

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from django_ergo.bots.runtime import Bot
    from django_ergo.conversation.adapters import ToolAdapter
    from django_ergo.conversation.models import ConversationSession

logger = logging.getLogger(__name__)

_JSON_TYPES = {
    str: "string",
    int: "integer",
    float: "number",
    bool: "boolean",
    list: "array",
    dict: "object",
}


@dataclass
class ToolContext:
    """What a tool knows about the call: which bot, session and user."""

    bot: Bot | None = None
    session: ConversationSession | None = None
    user: Any = None

    @property
    def tasks(self):
        """Start this bot's ``@bot_task`` functions in the background."""
        from django_ergo.bots.background import BotTasks

        return BotTasks(self.bot)

    @property
    def is_root(self) -> bool:
        return bool(self.session and self.session.parent_id is None)

    def secret(self, name: str, default: str | None = None) -> str | None:
        """An environment value, preferring the user's own ``NAME__<USERNAME>``."""
        if self.user is not None:
            username = re.sub(r"\W", "_", self.user.get_username()).upper()
            if value := os.environ.get(f"{name}__{username}"):
                return value
        return os.environ.get(name) or default

    @property
    def timezone(self):
        """The user's ``timezone`` attribute, else the bot's, else Django's."""
        from zoneinfo import ZoneInfo
        from zoneinfo import ZoneInfoNotFoundError

        from django.conf import settings

        names = [
            getattr(self.user, "timezone", None),
            self.bot.definition.timezone if self.bot else None,
            settings.TIME_ZONE,
        ]
        for name in names:
            if name and isinstance(name, str):
                try:
                    return ZoneInfo(name)
                except (ZoneInfoNotFoundError, ValueError):
                    continue
        return ZoneInfo("UTC")

    def now(self):
        from datetime import datetime

        return datetime.now(self.timezone)


@dataclass
class BotTool:
    name: str
    description: str
    function: Callable
    parameters: dict
    required: list[str]
    requires_approval: bool = False
    takes_context: bool = False

    def json_schema(self) -> dict:
        return {
            "type": "object",
            "properties": self.parameters,
            "required": self.required,
        }


def _infer_parameters(func: Callable, skip_first: bool) -> tuple[dict, list[str]]:
    signature = inspect.signature(func)
    try:
        hints = get_type_hints(func)
    except Exception:  # noqa: BLE001 — unresolvable annotations: fall back to strings
        hints = {}
    properties: dict[str, dict] = {}
    required: list[str] = []
    params = list(signature.parameters.values())
    if skip_first and params:
        params = params[1:]
    for param in params:
        if param.kind in (param.VAR_POSITIONAL, param.VAR_KEYWORD):
            continue
        hint = hints.get(param.name, str)
        origin = getattr(hint, "__origin__", hint)
        properties[param.name] = {"type": _JSON_TYPES.get(origin, "string")}
        if param.default is inspect.Parameter.empty:
            required.append(param.name)
    return properties, required


def bot_tool(  # noqa: PLR0913
    func: Callable | None = None,
    *,
    name: str | None = None,
    description: str | None = None,
    parameters: dict | None = None,
    required: list[str] | None = None,
    requires_approval: bool = False,
    takes_context: bool = False,
):
    """Mark a function as a bot tool. Usable bare (@bot_tool) or with options."""

    def decorate(fn: Callable) -> Callable:
        if parameters is not None:
            props = parameters
            req = list(parameters.keys()) if required is None else list(required)
        else:
            props, req = _infer_parameters(fn, skip_first=takes_context)
        fn.__bot_tool__ = BotTool(
            name=name or fn.__name__,
            description=description or (inspect.getdoc(fn) or fn.__name__),
            function=fn,
            parameters=props,
            required=req,
            requires_approval=requires_approval,
            takes_context=takes_context,
        )
        return fn

    return decorate(func) if func is not None else decorate


def bot_task(func: Callable | None = None, *, name: str | None = None):
    """Mark a function as a background task of the bot whose tool file defines it.

    Tools start it with ``ctx.tasks`` (see ``django_ergo.bots.background``).
    """

    def decorate(fn: Callable) -> Callable:
        fn.__bot_task__ = name or fn.__name__
        return fn

    return decorate(func) if func is not None else decorate


@dataclass
class BotContext:
    title: str
    function: Callable
    weight: float = 1.0

    def render(self, ctx: ToolContext, message: str) -> str:
        """The function's text; a failure is logged and gives no text."""
        try:
            return self.function(ctx, message) or ""
        except Exception:
            logger.exception("Bot context %r failed", self.title)
            return ""


def bot_context(
    func: Callable | None = None, *, title: str | None = None, weight: float = 1.0
):
    """Mark ``fn(ctx, message) -> str`` as context for every turn."""

    def decorate(fn: Callable) -> Callable:
        heading = title or fn.__name__.replace("_", " ").capitalize()
        fn.__bot_context__ = BotContext(heading, fn, weight)
        return fn

    return decorate(func) if func is not None else decorate


class FunctionToolkit(Toolkit):
    """A Toolkit made of @bot_tool functions, bound to a ToolContext."""

    def __init__(
        self,
        tools: list[BotTool],
        context: ToolContext | None = None,
        *,
        seed: list[str] | None = None,
    ):
        self.tools = {tool.name: tool for tool in tools}
        if len(self.tools) != len(tools):
            msg = "Duplicate bot tool names"
            raise ValueError(msg)
        self.context = context or ToolContext()
        # Tools (taking no arguments) whose results start every session.
        self.seed = [name for name in seed or [] if name in self.tools]

    def pre_seeds(self):
        from django_ergo.conversation.structured import PreSeedCall

        return [
            PreSeedCall(
                name,
                {},
                lambda arguments, name=name: self.execute_tool(name, arguments),
            )
            for name in self.seed
        ]

    @classmethod
    def from_functions(
        cls, functions: list[Callable], context: ToolContext | None = None
    ) -> FunctionToolkit:
        tools = []
        for fn in functions:
            tool = getattr(fn, "__bot_tool__", None)
            if tool is None:
                bot_tool(fn)
                tool = fn.__bot_tool__
            tools.append(tool)
        return cls(tools, context)

    def has_tool(self, tool_name: str) -> bool:
        return tool_name in self.tools

    def requires_approval(self, tool_name: str) -> bool:
        return self.tools[tool_name].requires_approval

    def get_tools_schema(self, adapter: ToolAdapter) -> list[dict]:
        schemas = []
        for tool in self.tools.values():
            if isinstance(adapter, OpenAIToolAdapter):
                schemas.append(
                    {
                        "type": "function",
                        "function": {
                            "name": tool.name,
                            "description": tool.description,
                            "parameters": tool.json_schema(),
                        },
                    }
                )
            else:
                schemas.append(
                    {
                        "name": tool.name,
                        "description": tool.description,
                        "input_schema": tool.json_schema(),
                    }
                )
        return schemas

    def execute_tool(self, tool_name: str, arguments: dict) -> str:
        tool = self.tools.get(tool_name)
        if tool is None:
            msg = f"Unknown tool: {tool_name}"
            raise ValueError(msg)
        args = [self.context] if tool.takes_context else []
        result = tool.function(*args, **(arguments or {}))
        if isinstance(result, str):
            return result
        return json.dumps(result, default=str)

    def render_overview(self) -> str:
        return ""


@dataclass
class ToolModule:
    path: Path
    tools: list[BotTool]
    toolkit_factory: Callable | None = None
    contexts: list[BotContext] = field(default_factory=list)
    tasks: dict[str, Callable] = field(default_factory=dict)
    module: Any = None  # the imported module (its docstring describes the skill)


def load_tool_module(path: Path, bot_name: str) -> ToolModule:
    """Import one tool file and collect its @bot_tool functions."""
    digest = hashlib.sha1(str(path).encode(), usedforsecurity=False).hexdigest()[:8]
    module_name = f"ergo_bot_tools.{bot_name}.{path.stem}_{digest}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        msg = f"Cannot load tool module {path}"
        raise ImportError(msg)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    tools = [
        value.__bot_tool__
        for value in vars(module).values()
        if callable(value) and hasattr(value, "__bot_tool__")
    ]
    contexts = [
        value.__bot_context__
        for value in vars(module).values()
        if callable(value) and hasattr(value, "__bot_context__")
    ]
    tasks = {
        value.__bot_task__: value
        for value in vars(module).values()
        if callable(value) and hasattr(value, "__bot_task__")
    }
    factory = getattr(module, "toolkits", None)
    return ToolModule(
        path=path,
        tools=tools,
        toolkit_factory=factory,
        contexts=contexts,
        tasks=tasks,
        module=module,
    )
