"""Kubectl plugin: run configured-cluster commands without exposing kubeconfig data.

    plugins:
      - name: kubectl
        clusters:
          cluster-name:
            kubeconfig: /mounted/kubeconfig
            namespace: default       # optional default for calls without -n/--namespace
        approve: true                # kubectl_run waits for every approval
        timeout: 120                 # seconds per command
        root_only: true              # only the root session gets these tools

``kubectl_read`` only permits inspection verbs (``get``, ``describe``,
``logs``, ``top``, ``events``, ``explain``, ``api-resources``, ``version`` and
``auth can-i``) without approval. ``kubectl_run`` handles every other verb and
requires approval unless ``approve: false``. Both tools require the configured
cluster name and execute an argv list with that cluster's kubeconfig.

Secret bodies are never returned: structured ``data`` and ``stringData`` fields
are redacted from every command result, and ``get secret`` rejects custom output
formats that could select a value.
"""

from __future__ import annotations

import json
import re
import subprocess
from typing import TYPE_CHECKING
from typing import Any

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

MAX_OUTPUT_CHARS = 40_000
READ_ONLY_VERBS = {
    "get",
    "describe",
    "logs",
    "top",
    "events",
    "explain",
    "api-resources",
    "version",
}
DENIED_FLAGS = {
    "--as",
    "--as-group",
    "--as-uid",
    "--as-user-extra",
    "--certificate-authority",
    "--client-certificate",
    "--client-key",
    "--context",
    "--disable-compression",
    "--insecure-skip-tls-verify",
    "--kubeconfig",
    "--password",
    "--request-timeout",
    "--server",
    "--token",
    "--user",
    "--username",
}
DENIED_READ_FLAGS = {"--raw"}
NAMESPACE_FLAGS = {"--namespace", "-n"}
ALL_NAMESPACES_FLAGS = {"--all-namespaces", "-A"}
SECRET_DATA_KEYS = {"data", "stringdata"}
DATA_KEY_LINE = re.compile(
    r"^(?P<indent>\s*)(?P<list>-\s+)?(?P<key>['\"]?(?:data|stringData)['\"]?)\s*:"
)

ARGS_SCHEMA = {
    "cluster": {
        "type": "string",
        "description": "Configured cluster name from this bot's kubectl plugin settings",
    },
    "args": {
        "type": "array",
        "items": {"type": "string"},
        "description": 'Arguments after kubectl, for example ["get", "pods"]',
    },
}


def command_of(args: list[str]) -> tuple[str, ...]:
    """Return an exact kubectl command, never interpreting aliases or packed args."""
    if not args or args[0].startswith("-"):
        return ()
    if args[0] == "auth" and len(args) > 1 and not args[1].startswith("-"):
        return "auth", args[1]
    return (args[0],)


def is_read_only(args: list[str]) -> bool:
    """Whether ``args`` is one of the intentionally small inspection command set."""
    command = command_of(args)
    return command in {(verb,) for verb in READ_ONLY_VERBS} or command == (
        "auth",
        "can-i",
    )


def _flag_name(arg: str) -> str:
    return arg.split("=", 1)[0]


def _has_flag(args: list[str], flags: set[str]) -> bool:
    return any(_flag_name(arg) in flags or arg in flags for arg in args)


def _has_namespace(args: list[str]) -> bool:
    return _has_flag(args, NAMESPACE_FLAGS | ALL_NAMESPACES_FLAGS) or any(
        arg.startswith("-n") and arg != "-n" for arg in args
    )


def _secret_resource(token: str) -> bool:
    """Recognize singular, plural and group-qualified Secret resource spellings."""
    for resource_token in token.lower().split(","):
        resource = resource_token.split("/", 1)[0].split(".", 1)[0]
        if resource in {"secret", "secrets"}:
            return True
    return False


def _reads_secret(args: list[str]) -> bool:
    return command_of(args) == ("get",) and any(
        not arg.startswith("-") and _secret_resource(arg) for arg in args[1:]
    )


def _secret_output_is_safe(args: list[str]) -> bool:
    """Only kubectl's no-body table/name formats are allowed for ``get secret``."""
    for index, arg in enumerate(args):
        name = _flag_name(arg)
        if name in {"--template", "--custom-columns"}:
            return False
        if name not in {"-o", "--output"} and not arg.startswith("-o"):
            continue
        if name in {"-o", "--output"}:
            if "=" in arg:
                value = arg.split("=", 1)[1]
            elif index + 1 < len(args):
                value = args[index + 1]
            else:
                return False
        else:
            value = arg[2:]
            if value.startswith("="):
                value = value[1:]
        if value.lower() not in {"name", "wide"}:
            return False
    return True


def _get_can_flatten_output(args: list[str]) -> bool:
    """Whether a get formatter could extract a Secret value without its data key."""
    for index, arg in enumerate(args):
        name = _flag_name(arg)
        if name in {"--template", "--custom-columns"}:
            return True
        if name not in {"-o", "--output"} and not arg.startswith("-o"):
            continue
        if name in {"-o", "--output"}:
            value = (
                arg.split("=", 1)[1]
                if "=" in arg
                else args[index + 1]
                if index + 1 < len(args)
                else ""
            )
        else:
            value = arg[2:].lstrip("=")
        if value.lower().startswith(
            ("jsonpath", "go-template", "template", "custom-columns")
        ):
            return True
    return False


def _redact_data(value: Any) -> Any:
    if isinstance(value, list):
        return [_redact_data(item) for item in value]
    if isinstance(value, dict):
        return {
            key: "[REDACTED]"
            if str(key).replace("-", "").replace("_", "").lower() in SECRET_DATA_KEYS
            else _redact_data(item)
            for key, item in value.items()
        }
    return value


def _redact_yaml_data(text: str) -> str:
    """Remove indented YAML data blocks without requiring a YAML dependency."""
    output: list[str] = []
    data_indent: int | None = None
    for line in text.splitlines(keepends=True):
        indent = len(line) - len(line.lstrip(" \t"))
        if data_indent is not None:
            if line.strip() and indent <= data_indent:
                data_indent = None
            else:
                continue
        match = DATA_KEY_LINE.match(line)
        if match:
            output.append(
                f"{match.group('indent')}{match.group('list') or ''}{match.group('key')}: [REDACTED]\n"
            )
            data_indent = len(match.group("indent"))
        else:
            output.append(line)
    return "".join(output)


def redact_secret_data(text: str) -> str:
    """Redact Kubernetes Secret body fields from JSON and YAML-looking output."""
    try:
        value = json.loads(text)
    except ValueError:
        return _redact_yaml_data(text)
    return json.dumps(_redact_data(value), separators=(",", ":"), ensure_ascii=False)


class KubectlPlugin(BotPlugin):
    name = "kubectl"
    description = (
        "Inspect configured Kubernetes clusters and run approved kubectl changes"
    )

    def on_load(self) -> None:
        configured = self.config.get("clusters")
        if not isinstance(configured, dict) or not configured:
            msg = "kubectl requires a non-empty clusters mapping"
            raise ValueError(msg)
        self.clusters: dict[str, dict[str, str]] = {}
        for name, value in configured.items():
            if not isinstance(name, str) or not name or not isinstance(value, dict):
                msg = "each kubectl cluster must have a name and mapping configuration"
                raise ValueError(msg)
            kubeconfig = value.get("kubeconfig")
            namespace = value.get("namespace", "")
            if not isinstance(kubeconfig, str) or not kubeconfig:
                msg = "each kubectl cluster requires a kubeconfig path"
                raise ValueError(msg)
            if not isinstance(namespace, str):
                msg = "kubectl cluster namespace must be a string"
                raise TypeError(msg)
            self.clusters[name] = {"kubeconfig": kubeconfig, "namespace": namespace}
        self.executable = str(self.config.get("executable") or "kubectl")
        self.approve = bool(self.config.get("approve", True))
        self.root_only = bool(self.config.get("root_only", True))
        self.timeout = int(self.config.get("timeout", 120))

    def _cluster(self, cluster: str) -> dict[str, str]:
        try:
            return self.clusters[cluster]
        except KeyError as exc:
            msg = "That cluster is not configured for this bot."
            raise ValueError(msg) from exc

    def argv(self, cluster: str, args: list[str]) -> list[str]:
        if (
            not isinstance(args, list)
            or not args
            or not all(isinstance(arg, str) for arg in args)
        ):
            msg = 'Give kubectl arguments as a non-empty list, e.g. ["get", "pods"].'
            raise ValueError(msg)
        if any(not arg or "\x00" in arg for arg in args):
            msg = "kubectl arguments must be non-empty strings without NUL bytes."
            raise ValueError(msg)
        if _has_flag(args, DENIED_FLAGS):
            msg = "This bot pins kubeconfig and identity to the selected configured cluster."
            raise ValueError(msg)
        selected = self._cluster(cluster)
        argv = [self.executable, *args, "--kubeconfig", selected["kubeconfig"]]
        if selected["namespace"] and not _has_namespace(args):
            argv.extend(["--namespace", selected["namespace"]])
        return argv

    def execute(self, cluster: str, args: list[str]) -> str:
        if command_of(args) == ("get",) and _get_can_flatten_output(args):
            return "kubectl get does not permit output formats that can extract field values."
        if _reads_secret(args) and not _secret_output_is_safe(args):
            return "Secret reads do not permit custom output formats; use the default table or -o name."
        try:
            proc = subprocess.run(  # noqa: S603 -- argv list, no shell
                self.argv(cluster, args),
                capture_output=True,
                text=True,
                timeout=self.timeout,
                stdin=subprocess.DEVNULL,
                check=False,
            )
        except FileNotFoundError:
            return f"The kubectl CLI ({self.executable}) is not installed on this host."
        except subprocess.TimeoutExpired as exc:
            partial = (exc.stdout or "") + (exc.stderr or "")
            if isinstance(partial, bytes):
                partial = partial.decode(errors="replace")
            return trim(
                redact_secret_data(f"Timed out after {self.timeout}s.\n{partial}"),
                MAX_OUTPUT_CHARS,
            )
        output = redact_secret_data((proc.stdout + proc.stderr).strip())
        return trim(
            f"Exit {proc.returncode}\n{output or '(no output)'}", MAX_OUTPUT_CHARS
        )

    def read(self, cluster: str, args: list[str]) -> str:
        self._cluster(cluster)
        if not is_read_only(args) or _has_flag(args, DENIED_READ_FLAGS):
            return "kubectl_read only permits configured read-only verbs; use kubectl_run for other commands."
        return self.execute(cluster, args)

    def run(self, cluster: str, args: list[str]) -> str:
        self._cluster(cluster)
        if is_read_only(args):
            return "Use kubectl_read for read-only commands."
        return self.execute(cluster, args)

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
            "Each kubectl_run call waits for approval."
            if self.approve
            else "kubectl_run calls do not wait for approval."
        )
        return [
            TextContextSource(
                "Kubernetes",
                "kubectl_read can inspect only the configured clusters "
                f"({', '.join(self.clusters)}). kubectl_run changes a selected cluster. "
                f"{approval} Pass arguments as a list, never a shell command. Secret data is never returned.",
            )
        ]

    def _tools(self) -> list[BotTool]:
        plugin = self

        @bot_tool(
            name="kubectl_read",
            description=(
                "Run a read-only kubectl command against one configured cluster: get, describe, logs, "
                "top, events, explain, api-resources, version, or auth can-i. Secret body output is blocked."
            ),
            parameters=ARGS_SCHEMA,
            required=["cluster", "args"],
        )
        def read(cluster: str, args: list[str]) -> str:
            return plugin.read(cluster, args)

        @bot_tool(
            name="kubectl_run",
            description=(
                "Run a non-read-only kubectl command against one configured cluster. Each call changes or "
                "may change state and requires approval unless this bot explicitly disables it."
            ),
            parameters=ARGS_SCHEMA,
            required=["cluster", "args"],
            requires_approval=self.approve,
        )
        def run(cluster: str, args: list[str]) -> str:
            return plugin.run(cluster, args)

        return [read.__bot_tool__, run.__bot_tool__]
