"""Review regressions: fake credentials and local subprocesses only.

The independent tests/test_pr418_review.py in the review worktree is read-only;
its bug-reproducing assertions intentionally describe the old head.
"""

import json
import subprocess
import sys
from types import SimpleNamespace

import pytest

from django_ergo.conversation.engines.claude_code import child_env as claude_env
from django_ergo.conversation.engines.codex_cli import child_env as codex_env
from django_ergo.plugins.local_shell import LocalShellError
from django_ergo.plugins.local_shell import LocalShellPlugin
from django_ergo.protected_env import scrubbed_env
from django_ergo.provider_credentials import register_credential_source


@pytest.mark.parametrize("factory", [scrubbed_env, claude_env, codex_env])
def test_provider_variants_and_file_pointers_never_reach_grandchildren(
    factory, monkeypatch
):
    names = [
        "RYAN_MINIMAX_API_KEY",
        "ANTHROPIC_STRUCTURED_API_KEY",
        "OPENAI_ADMIN_KEY",
        "AZURE_OPENAI_KEY",
        "GOOGLE_GENERATIVE_AI_API_KEY",
        "GEMINI_KEY",
        "XAI_MANAGEMENT_KEY",
        "OPENROUTER_KEY",
        "GOOGLE_APPLICATION_CREDENTIALS_JSON",
        "ADS_GOOGLE_SERVICE_ACCOUNT_JSON",
        "GOOGLE_APPLICATION_CREDENTIALS",
        "PREFIX_GCPJSON",
        "CUSTOM_PRIVATE_KEY_FILE",
        "AWS_WEB_IDENTITY_TOKEN_FILE",
    ]
    for name in names:
        monkeypatch.setenv(name, "fake-or-fake-file-path")
    probe = "import json,os; print(json.dumps(sorted(os.environ)))"
    parent = "import subprocess,sys; subprocess.run([sys.executable,'-c',sys.argv[1]],check=True)"
    result = subprocess.run(
        [sys.executable, "-c", parent, probe],
        env=factory(),
        capture_output=True,
        text=True,
        check=True,
    )
    assert not set(names).intersection(json.loads(result.stdout))


def test_broken_bot_isolated_and_unknown_alias_denied(tmp_path, monkeypatch):
    from django_ergo.bots.registry import BotRegistry
    from django_ergo.plugins.bash import BashPlugin

    good, bad = tmp_path / "good", tmp_path / "bad"
    good.mkdir()
    bad.mkdir()
    (good / "bot.yaml").write_text("name: good\nengine: {type: claude}\n")
    path = bad / "bot.yaml"
    path.write_text("engine: {api_key_env: UNSEEN_ALIAS\n")
    registry = BotRegistry.from_paths([good, bad], skip_broken=True)
    assert "good" in registry.bots and registry.failed
    monkeypatch.setenv("UNSEEN_ALIAS", "fake")
    for factory in (scrubbed_env, claude_env, codex_env):
        assert "UNSEEN_ALIAS" not in factory()
        assert "HOME" in factory()
    bash = BashPlugin(registry.bots["good"], {"cwd": str(tmp_path)})
    bash.on_load()
    assert "safe" in str(bash.run("printf safe"))
    path.write_text("engine: {api_key_env: UNSEEN_ALIAS}\n")
    assert "UNSEEN_ALIAS" not in scrubbed_env()
    path.unlink()
    assert "UNSEEN_ALIAS" not in scrubbed_env()


def test_cached_sources_retain_names_on_read_errors(tmp_path, monkeypatch):
    path = tmp_path / "bot.yaml"
    path.write_text("api_key_env: CACHED_ALIAS\n")
    register_credential_source(path)
    assert scrubbed_env({"CACHED_ALIAS": "fake"}) == {}
    original = type(path).read_text
    reads = []

    def read(self, *args, **kwargs):
        reads.append(self)
        if self == path:
            message = "synthetic"
            raise PermissionError(message)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(type(path), "read_text", read)
    assert scrubbed_env({"CACHED_ALIAS": "fake"}) == {}
    assert path in reads  # read failures fail closed even with unchanged metadata
    path.write_text("api_key_env: NEW_UNKNOWN_ALIAS\n")
    assert scrubbed_env(
        {"CACHED_ALIAS": "fake", "NEW_UNKNOWN_ALIAS": "fake", "HOME": "/fake"}
    ) == {"HOME": "/fake"}


@pytest.mark.parametrize(
    "secret",
    [
        "GEMINI_API_KEY",
        "MINIMAX_API_KEY",
        "RYAN_MINIMAX_API_KEY",
        "BOUNDCORP_ORG_OPENAI_KEY",
        "GOOGLE_APPLICATION_CREDENTIALS",
    ],
)
def test_media_named_credentials_require_trusted_job_boundary(secret):
    plugin = LocalShellPlugin(None, {"secrets": [secret]})
    plugin.on_load()
    with pytest.raises(
        LocalShellError, match="coding subprocess.*trusted per-job allowlist"
    ):
        plugin.env(SimpleNamespace(secret=lambda name: "fake"))


def test_fixed_argv_video_does_not_inherit_credentials(monkeypatch):
    from django_ergo.conversation import video

    monkeypatch.setenv("RYAN_MINIMAX_API_KEY", "fake")
    monkeypatch.setattr(video.shutil, "which", lambda _: "/fake/ffmpeg")
    seen = []
    monkeypatch.setattr(
        video.subprocess, "run", lambda *a, **kw: seen.append(kw["env"])
    )
    video._run(["ffmpeg", "-version"], 2)
    assert "RYAN_MINIMAX_API_KEY" not in seen[0]


def test_same_metadata_rewrite_is_discovered_without_reparsing_unchanged_yaml(
    tmp_path, monkeypatch
):
    import yaml

    path = tmp_path / "providers.yaml"
    path.write_text("api_key_env: ALIAS_BEFORE\n")
    register_credential_source(path)
    assert scrubbed_env({"ALIAS_BEFORE": "fake"}) == {}
    stamp = path.stat()
    original_stat = type(path).stat
    original_load = yaml.safe_load
    parsed = []

    def stat(self, *args, **kwargs):
        return stamp if self == path else original_stat(self, *args, **kwargs)

    def load(content):
        parsed.append(content)
        return original_load(content)

    monkeypatch.setattr(type(path), "stat", stat)
    monkeypatch.setattr(yaml, "safe_load", load)
    assert scrubbed_env({"ALIAS_BEFORE": "fake"}) == {}
    assert "api_key_env: ALIAS_BEFORE\n" not in parsed
    path.write_text("api_key_env: ALIAS_AFTER_\n")
    assert scrubbed_env({"ALIAS_AFTER_": "fake", "ALIAS_BEFORE": "fake"}) == {}
    assert "api_key_env: ALIAS_AFTER_\n" in parsed


def test_transient_read_failure_recovers_without_content_change(tmp_path, monkeypatch):
    path = tmp_path / "bot.yaml"
    path.write_text("api_key_env: KNOWN_ALIAS\n")
    register_credential_source(path)
    probe = {"KNOWN_ALIAS": "fake", "PUBLIC_VAR": "kept"}
    assert scrubbed_env(probe) == {"PUBLIC_VAR": "kept"}
    original = type(path).read_text

    def unreadable(self, *args, **kwargs):
        if self == path:
            message = "synthetic denial"
            raise PermissionError(message)
        return original(self, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(type(path), "read_text", unreadable)
        assert scrubbed_env(probe) == {}
    assert scrubbed_env(probe) == {"PUBLIC_VAR": "kept"}
