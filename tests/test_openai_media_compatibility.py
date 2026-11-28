"""Name-only live-config controls; all values and child commands are fake.

Lists match ergo-bots main 68b24ff6ab5954a8f48ed95209cec04b2db15f81,
Ads bot.yaml:37 and Storytime bot.yaml:204 (PR418 independent rereview).
This file also runs unchanged on exact main as a compatibility control.
"""

import json
import subprocess
import sys
from types import SimpleNamespace

import pytest

from django_ergo.plugins.local_shell import RUNNER
from django_ergo.plugins.local_shell import LocalShellPlugin

MEDIA_LISTS = [
    ["ELEVENLABS_API_KEY", "SUME_API_KEY", "GEMINI_API_KEY", "MINIMAX_API_KEY"],
    ["GEMINI_API_KEY", "SUME_API_KEY", "ELEVENLABS_API_KEY", "RYAN_MINIMAX_BASE_URL"],
]


@pytest.mark.parametrize("secrets", MEDIA_LISTS, ids=["ads", "storytime"])
@pytest.mark.parametrize("agent", ["claude", "codex"])
@pytest.mark.parametrize("run", [1, 2], ids=["start", "resume"])
def test_live_media_lists_launch_local_fake_child(
    tmp_path, monkeypatch, secrets, agent, run
):
    monkeypatch.setenv("OPENAI_API_KEY", "fake-legacy")
    monkeypatch.setenv("BOUNDCORP_ORG_OPENAI_KEY", "fake-org")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "fake-subscription")
    plugin = LocalShellPlugin(None, {"secrets": secrets, "agents": [agent]})
    plugin.on_load()
    meta = tmp_path / ".ergo"
    meta.mkdir()
    (meta / f"run-{run}.json").write_text(
        json.dumps(
            {
                "argv": [
                    sys.executable,
                    "-c",
                    "import os,json; print(json.dumps(dict(os.environ)))",
                ],
                "timeout_seconds": 10,
            }
        )
    )
    subprocess.run(
        [sys.executable, str(RUNNER), str(tmp_path), str(run)],
        env=plugin.env(SimpleNamespace(secret=lambda name: "fake-media")),
        check=True,
    )
    env = json.loads((meta / f"run-{run}.jsonl").read_text())
    assert all(env[name] == "fake-media" for name in secrets)
    assert "OPENAI_API_KEY" not in env
    assert "BOUNDCORP_ORG_OPENAI_KEY" not in env
