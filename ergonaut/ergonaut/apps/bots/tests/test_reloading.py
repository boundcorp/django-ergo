import subprocess

import pytest

from ergonaut.apps.bots.reloading import ReloadingRegistry, pull_checkout


def write_bot(folder, name, description="A bot"):
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "bot.yaml").write_text(f"name: {name}\ndescription: {description}\n")
    (folder / "agents.md").write_text(f"# {name}\n")


@pytest.fixture
def bots_dir(tmp_path, monkeypatch):
    root = tmp_path / "bots"
    write_bot(root, "boundcorp")
    monkeypatch.setenv("ERGONAUT_BOTS", str(root))
    return root


def test_a_new_bot_folder_loads_without_a_restart(bots_dir):
    registry = ReloadingRegistry(check_every=0)
    assert [b.name for b in registry] == ["boundcorp"]
    write_bot(bots_dir / "cto", "cto")
    assert "cto" in registry
    assert registry.get("cto").definition.description == "A bot"


def test_a_broken_config_keeps_the_loaded_bots(bots_dir):
    registry = ReloadingRegistry(check_every=0)
    assert "boundcorp" in registry
    (bots_dir / "bot.yaml").write_text("name: [unclosed\n")
    assert [b.name for b in registry] == ["boundcorp"]
    write_bot(bots_dir, "boundcorp", description="Fixed")
    assert registry.get("boundcorp").definition.description == "Fixed"


def test_unchanged_files_are_not_reloaded(bots_dir):
    loads = []

    def loader():
        from ergonaut.apps.bots.loading import load_registry

        loads.append(1)
        return load_registry()

    registry = ReloadingRegistry(loader=loader, check_every=0)
    list(registry)
    list(registry)
    assert len(loads) == 1


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


def test_pull_checkout_fast_forwards_only_a_clean_checkout(tmp_path, monkeypatch):
    for key in ("GIT_AUTHOR_NAME", "GIT_COMMITTER_NAME"):
        monkeypatch.setenv(key, "Bot")
    for key in ("GIT_AUTHOR_EMAIL", "GIT_COMMITTER_EMAIL"):
        monkeypatch.setenv(key, "bot@example.com")
    remote = tmp_path / "remote.git"
    git(tmp_path, "init", "--bare", "-b", "main", str(remote))
    live = tmp_path / "live"
    git(tmp_path, "clone", str(remote), str(live))
    git(live, "checkout", "-b", "main")
    write_bot(live, "boundcorp")
    git(live, "add", "-A")
    git(live, "commit", "-m", "init")
    git(live, "push", "-u", "origin", "main")

    other = tmp_path / "other"
    git(tmp_path, "clone", str(remote), str(other))
    write_bot(other / "cto", "cto")
    git(other, "add", "-A")
    git(other, "commit", "-m", "add cto")
    git(other, "push")

    (live / "scratch.txt").write_text("local edit")
    assert pull_checkout(live) == "skipped: uncommitted changes"
    (live / "scratch.txt").unlink()
    assert pull_checkout(live) == "pulled"
    assert (live / "cto" / "bot.yaml").exists()


def test_bot_tasks_run_through_celery(bots_dir):
    from django_ergo.bots import webhooks
    from django_ergo.bots.tools import ToolContext

    from ergonaut.apps.bots.reloading import ReloadingRegistry

    (bots_dir / "tools").mkdir()
    (bots_dir / "tools" / "jobs.py").write_text(
        "from django_ergo.bots import bot_task\n\n@bot_task\ndef double(n: int) -> int:\n    return n * 2\n"
    )
    (bots_dir / "bot.yaml").write_text("name: boundcorp\ntools: [tools/jobs.py]\n")
    registry = ReloadingRegistry(check_every=0)
    webhooks.set_registry(registry)
    try:
        job = ToolContext(bot=registry.get("boundcorp")).tasks.start("double", 21)
        assert job.wait(timeout=5) == 42  # eager without a broker
    finally:
        from ergonaut.apps.bots.loading import load_registry

        webhooks.set_registry(load_registry)
