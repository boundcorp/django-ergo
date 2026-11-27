"""Run a command with provider credentials removed and loopback-only test egress."""

# ruff: noqa: INP001, S603
import os
import re
import subprocess
import sys

pattern = re.compile(
    r"OPENAI|ANTHROPIC|FIREWORKS|GEMINI|GOOGLE|GCP|XAI|OPENROUTER|BOUNDCORP|CODEX|AZURE|GROQ|MISTRAL|HF_TOKEN|TOGETHER|DEEPSEEK|MINIMAX|API_KEY|AUTH_TOKEN|ACCESS_TOKEN|CREDENTIAL|SERVICE_ACCOUNT|PRIVATE_KEY",
    re.I,
)
env = {k: v for k, v in os.environ.items() if not pattern.search(k)}
env["PATH"] = "/home/dev/.local/bin:" + env.get("PATH", "")
env.update(ERGO_TEST_ISOLATED="1", TEST_OPENAI="false", DJANGO_DEBUG="true")
raise SystemExit(subprocess.call(sys.argv[1:], env=env))
