"""``ergonaut upgrade`` and the pluggable upgraders (see ergonaut/upgrades)."""

import pytest

from ergonaut import upgrades
from ergonaut.apps.bots.management.commands import wait_idle
from ergonaut.upgrades import Release, backends, github

RELEASE = Release(tag="v1.2.0", sha="b" * 40)


class Recorder(upgrades.Upgrader):
    name = "recorder"
    calls: list = []

    def current_version(self):
        return "a" * 40

    def upgrade(self, release):
        self.calls.append(release)
        return f"rolled out {release.tag}"


@pytest.fixture
def env(monkeypatch, tmp_path):
    Recorder.calls = []
    monkeypatch.setattr(upgrades, "_state_path", lambda: tmp_path / "upgrade.json")
    monkeypatch.setattr(github, "latest", lambda repo, channel: RELEASE)
    monkeypatch.setattr(github, "compare", lambda repo, current, target: "ahead")
    monkeypatch.setattr(wait_idle, "busy", lambda workers=True: [])
    monkeypatch.setenv("ERGONAUT_UPGRADER", f"{__name__}:Recorder")
    return monkeypatch


def test_load_upgrader_builtin_dotted_and_file(tmp_path, monkeypatch):
    monkeypatch.delenv("ERGONAUT_UPGRADER", raising=False)
    assert upgrades.load_upgrader() is None
    assert isinstance(upgrades.load_upgrader("systemd"), backends.SystemdUpgrader)
    assert isinstance(upgrades.load_upgrader(f"{__name__}.Recorder"), Recorder)

    plugin = tmp_path / "k8s.py"
    plugin.write_text(
        "from ergonaut.upgrades import Upgrader\n"
        "class Kube(Upgrader):\n"
        "    name = 'kube'\n"
        "    def upgrade(self, release):\n"
        "        return 'patched'\n"
    )
    loaded = upgrades.load_upgrader(f"{plugin}:Kube")
    assert loaded.name == "kube" and loaded.upgrade(RELEASE) == "patched"


def test_upgrades_when_a_newer_release_is_out_and_nothing_runs(env):
    assert upgrades.run(quiet_for=0, poll=0) == "rolled out v1.2.0"
    assert Recorder.calls == [RELEASE]
    assert upgrades.load_state()["status"] == "started"


def test_waits_while_turns_or_workers_run(env):
    env.setattr(wait_idle, "busy", lambda workers=True: ["worker kitchen: Build"])
    result = upgrades.run(wait_timeout=0, quiet_for=0, poll=0)
    assert "waiting" in result
    assert Recorder.calls == []


def test_workers_can_be_left_out_of_the_idle_gate(env):
    def busy(workers=True):
        return [] if not workers else ["worker devbox: Survey"]

    env.setattr(wait_idle, "busy", busy)
    assert "waiting" in upgrades.run(wait_timeout=0, quiet_for=0, poll=0)
    env.setenv("ERGONAUT_UPGRADE_WAIT_FOR_WORKERS", "0")
    assert upgrades.run(wait_timeout=0, quiet_for=0, poll=0) == "rolled out v1.2.0"


@pytest.mark.parametrize("status", ["identical", "behind", "diverged"])
def test_leaves_up_to_date_or_newer_instances_alone(env, status):
    env.setattr(github, "compare", lambda repo, current, target: status)
    upgrades.run(quiet_for=0, poll=0)
    assert Recorder.calls == []


def test_a_failed_release_is_not_retried_every_tick(env):
    def boom(self, release):
        raise RuntimeError("rollout failed")

    env.setattr(Recorder, "upgrade", boom)
    with pytest.raises(RuntimeError):
        upgrades.run(quiet_for=0, poll=0)
    assert upgrades.load_state()["status"] == "failed"
    assert "failed recently" in upgrades.run(quiet_for=0, poll=0)


def test_not_ready_is_retried_next_time_not_failed(env):
    def later(self, release):
        raise upgrades.NotReady("image not published")

    env.setattr(Recorder, "upgrade", later)
    assert "isn't ready" in upgrades.run(quiet_for=0, poll=0)
    assert upgrades.load_state()["status"] == "waiting"
    env.setattr(Recorder, "upgrade", lambda self, release: "done")
    assert upgrades.run(quiet_for=0, poll=0) == "done"


def test_check_reports_without_upgrading(env):
    result = upgrades.check(upgrades.load_upgrader())
    assert result.available
    assert "upgrade available" in result.describe()
    assert Recorder.calls == []


def test_command_upgrader_gets_the_release_in_its_environment(monkeypatch):
    monkeypatch.setenv("ERGONAUT_UPGRADE_COMMAND", 'echo "deploy $ERGONAUT_UPGRADE_TAG $ERGONAUT_UPGRADE_SHA"')
    assert backends.CommandUpgrader().upgrade(RELEASE) == f"deploy v1.2.0 {'b' * 40}"


def test_systemd_restart_command(monkeypatch):
    monkeypatch.setenv("ERGONAUT_SYSTEMD_UNITS", "ergonaut-web ergonaut-worker")
    monkeypatch.setenv("ERGONAUT_SYSTEMD_USER", "1")
    assert backends.SystemdUpgrader().restart_command() == [
        "systemctl",
        "--user",
        "--no-block",
        "restart",
        "ergonaut-web",
        "ergonaut-worker",
    ]
    monkeypatch.setenv("ERGONAUT_SYSTEMD_RESTART", "sudo systemctl restart ergonaut")
    assert backends.SystemdUpgrader().restart_command() == ["sh", "-c", "sudo systemctl restart ergonaut"]


def test_systemd_moves_the_checkout_and_rolls_back_a_failed_install(tmp_path, monkeypatch):
    import subprocess

    origin = tmp_path / "origin"
    origin.mkdir()
    git = ["git", "-c", "user.email=t@t", "-c", "user.name=t"]
    subprocess.run([*git, "init", "-q", str(origin)], check=True)
    subprocess.run([*git, "-C", str(origin), "commit", "-q", "--allow-empty", "-m", "one"], check=True)
    first = subprocess.run(
        ["git", "-C", str(origin), "rev-parse", "HEAD"], capture_output=True, text=True
    ).stdout.strip()
    subprocess.run([*git, "-C", str(origin), "commit", "-q", "--allow-empty", "-m", "two"], check=True)
    second = subprocess.run(
        ["git", "-C", str(origin), "rev-parse", "HEAD"], capture_output=True, text=True
    ).stdout.strip()
    checkout = tmp_path / "checkout"
    subprocess.run(["git", "clone", "-q", str(origin), str(checkout)], check=True)
    subprocess.run(["git", "-C", str(checkout), "checkout", "-q", "--detach", first], check=True)

    monkeypatch.setenv("ERGONAUT_UPGRADE_CHECKOUT", str(checkout))
    monkeypatch.setenv("ERGONAUT_SYSTEMD_RESTART", "true")
    upgrader = backends.SystemdUpgrader()

    def head():
        return subprocess.run(
            ["git", "-C", str(checkout), "rev-parse", "HEAD"], capture_output=True, text=True
        ).stdout.strip()

    def broken_install(path):
        raise RuntimeError("pip failed")

    monkeypatch.setattr(upgrader, "install", broken_install)
    with pytest.raises(RuntimeError):
        upgrader.upgrade(Release(tag="v2", sha=second))
    assert head() == first

    monkeypatch.setattr(upgrader, "install", lambda path: None)
    assert "installed v2" in upgrader.upgrade(Release(tag="v2", sha=second))
    assert head() == second


def test_every_check_is_recorded_for_the_web_app(env):
    env.setattr(github, "compare", lambda repo, current, target: "identical")
    upgrades.run(quiet_for=0, poll=0)
    state = upgrades.load_state()
    assert state["checked_at"] and "up to date" in state["result"]

    def offline(repo, channel):
        raise OSError("network down")

    env.setattr(github, "latest", offline)
    with pytest.raises(OSError):
        upgrades.run(quiet_for=0, poll=0)
    assert upgrades.load_state()["result"] == "check failed: network down"


@pytest.mark.django_db
def test_version_endpoint_shows_admins_the_last_check(env, client, django_user_model):
    from django.core.cache import cache

    cache.clear()
    env.setenv("ERGONAUT_VERSION", "a" * 40)
    env.setattr(github, "commit", lambda repo, ref: (ref, "2026-10-04T18:00:00Z"))
    upgrades.save_state(checked_at=1.0, result="up to date")

    user = django_user_model.objects.create_user(username="cook", password="x")
    client.force_login(user)
    body = client.get("/api/version").json()
    assert body["commit"] == "a" * 40 and body["date"] == "2026-10-04T18:00:00Z"
    assert body["available"] and body["latest"]["sha"] == "b" * 40
    assert "last_check" not in body

    admin = django_user_model.objects.create_superuser(username="lee", password="x")
    client.force_login(admin)
    body = client.get("/api/version").json()
    assert body["last_check"]["result"] == "up to date"
    assert body["upgrader"].endswith(":Recorder")
