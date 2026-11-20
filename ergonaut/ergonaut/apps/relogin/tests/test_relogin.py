import logging
import os
import shlex
import sys
import threading
import time
from datetime import timedelta

import pytest
from django.test import Client
from django.utils import timezone

from ergonaut.apps.relogin import handoff, parsing, runner, stores
from ergonaut.apps.relogin.models import ReloginSession
from ergonaut.apps.relogin.tests.conftest import (
    AUTH_URL,
    CLAUDE_PASTE,
    CLAUDE_URL,
    CODEX_DEVICE,
    DEVICE_CODE,
    DEVICE_URL,
    OMP_ANTHROPIC,
    OMP_DEVICE,
    STATE,
    fake_cli,
)
from ergonaut.apps.users.factories import UserFactory

REDIRECT = f"http://localhost:54545/callback?code=c0de-XYZ_9&state={STATE}"
SECRETS = [AUTH_URL, CLAUDE_URL, STATE, DEVICE_CODE, "c0de-XYZ_9", "abcDEF123_xyz"]


# -- parsing -------------------------------------------------------------------------------


def test_find_url_only_allowed_hosts():
    hosts = frozenset({"claude.ai"})
    assert parsing.find_url(f"visit {AUTH_URL} now", hosts) == AUTH_URL
    assert parsing.find_url("https://claude.ai.evil.com/oauth?x=1", hosts) is None
    assert parsing.find_url("https://evil.com/?r=https://claude.ai", hosts) is None
    assert parsing.find_url("https://user@claude.ai/x", hosts) is None
    assert parsing.find_url("http://claude.ai/oauth", hosts) is None
    assert parsing.find_url("https://console.claude.ai/x", hosts) == "https://console.claude.ai/x"


def test_clean_strips_ansi_and_hyperlinks():
    raw = f"\x1b[1mgo\x1b[0m \x1b]8;;{AUTH_URL}\x1b\\{AUTH_URL}\x1b]8;;\x1b\\\r\n"
    assert parsing.clean(raw) == f"go {AUTH_URL}\n"


def test_device_code_and_expiry():
    text = parsing.clean(
        f"Open {DEVICE_URL}\n2. Enter this one-time code \x1b[90m(expires in 15 minutes)\x1b[0m\n   \x1b[94m{DEVICE_CODE}\x1b[0m\n"
    )
    shown = parsing.read_output(text, "device_code", frozenset({"auth.openai.com"}))
    assert (shown.url, shown.code, shown.seconds) == (DEVICE_URL, DEVICE_CODE, 900)
    assert parsing.find_code("Enter code: ABCD-EFGH") == "ABCD-EFGH"
    assert parsing.read_output(f"Open {DEVICE_URL}\n", "device_code", frozenset({"auth.openai.com"})) is None


def test_redirect_needs_state_in_link():
    no_state = AUTH_URL.replace(f"&state={STATE}", "")
    assert parsing.read_output(no_state, "paste_redirect", frozenset({"claude.ai"})) is None
    assert parsing.read_output(AUTH_URL, "paste_redirect", frozenset({"claude.ai"})).state == STATE


@pytest.mark.parametrize(
    "value",
    [
        f"https://localhost:54545/callback?code=x&state={STATE}",
        f"http://evil.com:54545/callback?code=x&state={STATE}",
        f"http://localhost:1455/callback?code=x&state={STATE}",
        f"http://localhost:54545/other?code=x&state={STATE}",
        f"http://localhost:54545/callback?state={STATE}",
        "http://localhost:54545/callback?code=x&state=wrong",
        f"http://localhost:54545/callback?code=x&state={STATE}&state={STATE}",
        f"http://u:p@localhost:54545/callback?code=x&state={STATE}",
        f"http://localhost:54545/callback?code=x&state={STATE} -K /etc/passwd",
        f"http://localhost:54545/callback?code=x&state={STATE}\nurl=http://evil",
        "",
    ],
)
def test_check_redirect_refuses(value):
    with pytest.raises(parsing.InputError) as exc:
        parsing.check_redirect(value, port=54545, state=STATE)
    assert "x&state" not in str(exc.value) and STATE not in str(exc.value)


def test_check_redirect_rebuilds_a_safe_url():
    url = parsing.check_redirect(REDIRECT.replace("localhost", "127.0.0.1") + '&x="q\\', port=54545, state=STATE)
    assert url.startswith("http://127.0.0.1:54545/callback?code=c0de-XYZ_9&state=")
    assert '"' not in url and "\\" not in url
    assert parsing.check_redirect(REDIRECT, port=54545, state=STATE).startswith("http://127.0.0.1:54545/")


def test_check_paste_code():
    assert parsing.check_paste_code(" abcDEF123_xyz#st4te ") == "abcDEF123_xyz#st4te"
    for bad in ["short", "abc def ghi jkl", "abcdefghijk\nrm -rf", "abcdefghijk;id", "abcdefghijk$(id)"]:
        with pytest.raises(parsing.InputError):
            parsing.check_paste_code(bad)


# -- the allow-list ------------------------------------------------------------------------


def test_only_allow_listed_commands_are_offered(bot):
    offered = stores.bot_stores(bot)
    assert set(offered) == {"omp/anthropic", "claude-cli", "codex-cli"}
    with pytest.raises(stores.StoreError):
        stores.load("devbox", "evil")
    assert offered["claude-cli"].argv == ("claude", "auth", "login", "--claudeai")
    assert offered["omp/anthropic"].port_group == "port:54545"


def test_url_hosts_are_cut_to_the_global_list(bot):
    bot.module.RELOGIN_STORES = {
        **bot.module.RELOGIN_STORES,
        "codex-cli": {**bot.module.RELOGIN_STORES["codex-cli"], "url_hosts": ["evil.com", "auth.openai.com"]},
    }
    assert stores.load("devbox", "codex-cli").url_hosts == frozenset({"auth.openai.com"})


def test_remote_command_is_fixed_and_quoted():
    remote = runner.remote_command(("omp", "login", "anthropic"))
    assert remote.startswith("exec env PATH=") and remote.endswith(" omp login anthropic")
    words = shlex.split(remote)
    assert words[words.index("-c") + 1] == runner.RELAY and words[-3:] == ["omp", "login", "anthropic"]
    with pytest.raises(stores.StoreError):
        runner.remote_command(("bash", "-c", "id"))


def test_ssh_argv_pins_host_and_ends_options(bot, tmp_path):
    target = stores.ssh_target(bot)
    with runner.known_hosts(target) as path:
        argv = runner.ssh_argv(target, path, runner.remote_command(("omp", "login", "anthropic")))
        assert open(path).read().startswith("[devbox-ssh.test]:2222 ssh-ed25519 ")
    assert not os.path.exists(path)
    assert argv[argv.index("--") + 1] == "devbox-ssh.test"
    assert "StrictHostKeyChecking=yes" in argv and "BatchMode=yes" in argv and "-T" in argv and "-tt" not in argv
    bot.module.RELOGIN_SSH = {**bot.module.RELOGIN_SSH, "host": "-oProxyCommand=id"}
    with pytest.raises(stores.StoreError):
        stores.ssh_target(bot)


# -- views ---------------------------------------------------------------------------------


def _session(user, store="omp/anthropic", **fields):
    spec = stores.load("devbox", store)
    defaults = {
        "user": user,
        "bot_name": "devbox",
        "store": store,
        "command": spec.command,
        "shape": spec.shape,
        "port_group": spec.port_group,
        "expires_at": timezone.now() + timedelta(seconds=spec.max_seconds),
    }
    return ReloginSession.objects.create(**{**defaults, **fields})


def test_anonymous_and_non_staff_are_refused(db, bot, redis, client):
    response = client.get("/relogin/?bot=devbox&store=claude-cli")
    assert response.status_code == 302 and response["Location"].startswith("/login?")
    client.force_login(UserFactory(is_staff=False))
    assert client.get("/relogin/?bot=devbox&store=claude-cli").status_code == 404
    assert client.post("/relogin/start/", {"bot": "devbox", "store": "claude-cli"}).status_code == 404
    assert not ReloginSession.objects.exists()


def test_start_needs_csrf(staff, bot, redis):
    strict = Client(enforce_csrf_checks=True)
    strict.force_login(staff)
    assert strict.post("/relogin/start/", {"bot": "devbox", "store": "claude-cli"}).status_code == 403
    assert not ReloginSession.objects.exists()


def test_start_creates_one_session_per_store_and_port(
    staff, bot, redis, client, monkeypatch, django_capture_on_commit_callbacks
):
    queued = []
    monkeypatch.setattr("ergonaut.apps.relogin.tasks.run_relogin.delay", queued.append)
    page = client.get("/relogin/?bot=devbox&store=omp/anthropic")
    assert page.status_code == 200 and b"Start re-login" in page.content
    with django_capture_on_commit_callbacks(execute=True):
        response = client.post("/relogin/start/", {"bot": "devbox", "store": "omp/anthropic"})
    session = ReloginSession.objects.get()
    assert response["Location"] == f"/relogin/{session.pk}/"
    assert queued == [session.pk]  # the task gets the session id and nothing else
    assert (session.user, session.command, session.port_group) == (staff, "omp login anthropic", "port:54545")
    assert session.expires_at <= timezone.now() + timedelta(seconds=300)
    again = client.post("/relogin/start/", {"bot": "devbox", "store": "omp/anthropic"})
    assert again["Location"] == f"/relogin/{session.pk}/" and ReloginSession.objects.count() == 1
    # Another store on the same callback port must wait too.
    bot.module.RELOGIN_STORES = {**bot.module.RELOGIN_STORES, "other": {**bot.module.RELOGIN_STORES["omp/anthropic"]}}
    client.post("/relogin/start/", {"bot": "devbox", "store": "other"})
    assert ReloginSession.objects.count() == 1


def test_unknown_store_or_command_refused(staff, bot, redis, client):
    assert client.post("/relogin/start/", {"bot": "devbox", "store": "evil"}).status_code == 404
    assert client.post("/relogin/start/", {"bot": "nope", "store": "claude-cli"}).status_code == 404
    assert not ReloginSession.objects.exists()


def test_sessions_belong_to_their_user(staff, bot, redis, client):
    other = _session(UserFactory(is_staff=True))
    for path in ["", "status"]:
        assert client.get(f"/relogin/{other.pk}/{path}").status_code == 404
    assert client.post(f"/relogin/{other.pk}/input", {"value": REDIRECT}).status_code == 404
    assert client.post(f"/relogin/{other.pk}/cancel").status_code == 404


def test_status_shows_link_only_while_waiting(staff, bot, redis, client):
    session = _session(staff)
    handoff.show(session.pk, {"url": AUTH_URL, "code": None, "state": STATE}, ttl=60)
    assert "url" not in client.get(f"/relogin/{session.pk}/status").json()
    ReloginSession.objects.filter(pk=session.pk).update(state="waiting")
    data = client.get(f"/relogin/{session.pk}/status").json()
    assert data["url"] == AUTH_URL and data["state"] == "waiting"  # the recorded OAuth state stays server-side
    assert "no-cache" in client.get(f"/relogin/{session.pk}/status")["Cache-Control"]


def test_submit_redirect_is_checked_and_single_use(staff, bot, redis, client, caplog):
    caplog.set_level(logging.DEBUG)
    session = _session(staff, state="waiting")
    handoff.show(session.pk, {"url": AUTH_URL, "code": None, "state": STATE}, ttl=60)
    bad = client.post(f"/relogin/{session.pk}/input", {"value": REDIRECT.replace(STATE, "zzQQ9wrong")})
    assert bad.status_code == 400 and b"zzQQ9wrong" not in bad.content
    ok = client.post(f"/relogin/{session.pk}/input", {"value": REDIRECT})
    assert ok.status_code == 200
    key = f"relogin:{session.pk}:input"
    assert redis.data[key].decode().startswith("http://127.0.0.1:54545/callback?code=c0de-XYZ_9")
    assert redis.ttl[key] == 60
    assert client.post(f"/relogin/{session.pk}/input", {"value": REDIRECT}).status_code == 409
    assert handoff.take_input(session.pk) and handoff.take_input(session.pk) is None  # read-and-delete
    assert not any(secret in caplog.text for secret in SECRETS)


def test_submit_paste_code(staff, bot, redis, client):
    session = _session(staff, store="claude-cli", state="waiting")
    handoff.show(session.pk, {"url": CLAUDE_URL, "code": None, "state": STATE}, ttl=60)
    for bad in ["bad code; id", "abcDEF123_xyz", "abcDEF123_xyz#otherstate"]:
        assert client.post(f"/relogin/{session.pk}/input", {"value": bad}).status_code == 400
    assert client.post(f"/relogin/{session.pk}/input", {"value": f"abcDEF123_xyz#{STATE}"}).status_code == 200
    device = _session(staff, store="codex-cli", state="waiting")
    assert client.post(f"/relogin/{device.pk}/input", {"value": "abcDEF123_xyz"}).status_code == 409


# -- the worker ----------------------------------------------------------------------------


def _relayed(argv):
    """The fake CLI behind the same PTY relay that runs on the login host."""
    return runner.Remote([sys.executable, "-c", runner.RELAY, *argv])


def _run(session, argv, **kwargs):
    runner.run(session.pk, spawn=lambda ssh: _relayed(argv), **kwargs)
    session.refresh_from_db()
    return session


def _no_secrets(session, caplog, redis):
    row = " ".join(str(getattr(session, f.name)) for f in ReloginSession._meta.fields)
    for secret in SECRETS:
        assert secret not in row
        assert secret not in caplog.text
    assert not any(k.endswith((":show", ":input")) for k in redis.data)


def test_device_flow_shows_link_and_code_then_checks(staff, bot, redis, tmp_path, caplog, monkeypatch):
    caplog.set_level(logging.DEBUG)
    session = _session(staff, store="codex-cli")
    seen = {}
    original = handoff.show
    monkeypatch.setattr(handoff, "show", lambda sid, data, ttl: seen.update(data) or original(sid, data, ttl))
    session = _run(session, fake_cli(tmp_path, CODEX_DEVICE))
    assert (seen["url"], seen["code"]) == (DEVICE_URL, DEVICE_CODE)
    assert (session.outcome, session.exit_code, session.account) == ("ok", 0, "same")
    assert session.post_check["ok"] is True
    assert bot.calls["identity"] == ["codex-cli"] and bot.calls["post_check"] == [("codex-cli", "hash-before")]
    _no_secrets(session, caplog, redis)


def test_omp_device_flow(staff, bot, redis, tmp_path, monkeypatch):
    bot.module.RELOGIN_STORES = {
        **bot.module.RELOGIN_STORES,
        "omp/openai-codex": {
            "command": "omp login openai-codex-device",
            "shape": "device_code",
            "url_hosts": ["auth.openai.com"],
        },
    }
    seen = {}
    original = handoff.show
    monkeypatch.setattr(handoff, "show", lambda sid, data, ttl: seen.update(data) or original(sid, data, ttl))
    session = _run(_session(staff, store="omp/openai-codex"), fake_cli(tmp_path, OMP_DEVICE))
    assert (seen["url"], seen["code"], session.outcome) == (DEVICE_URL, DEVICE_CODE, "ok")


def test_moved_callback_port_fails_closed(staff, bot, redis, tmp_path):
    moved = OMP_ANTHROPIC.replace("localhost%3A54545", "localhost%3A40001")
    session = _run(_session(staff, store="omp/anthropic"), fake_cli(tmp_path, moved))
    assert session.outcome == "failed" and session.error == runner.WRONG_PORT


def test_paste_code_goes_to_stdin(staff, bot, redis, tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    session = _session(staff, store="claude-cli")

    def paste():
        for _ in range(100):
            if f"relogin:{session.pk}:show" in redis.data:  # the worker showed the link
                handoff.put_input(session.pk, f"abcDEF123_xyz#{STATE}")
                return
            time.sleep(0.1)

    thread = threading.Thread(target=paste)
    thread.start()
    session = _run(session, fake_cli(tmp_path, CLAUDE_PASTE))
    thread.join()
    assert (session.outcome, session.exit_code) == ("ok", 0)
    _no_secrets(session, caplog, redis)


def test_redirect_is_relayed_not_typed(staff, bot, redis, tmp_path, monkeypatch):
    session = _session(staff, store="omp/anthropic")
    relayed = []
    monkeypatch.setattr(runner, "relay_redirect", lambda target, kh, url: relayed.append(url) or True)
    checked = parsing.check_redirect(REDIRECT, port=54545, state=STATE)

    def paste():
        for _ in range(100):
            if f"relogin:{session.pk}:show" in redis.data:  # the worker showed the link
                handoff.put_input(session.pk, checked)
                time.sleep(0.5)
                handoff.cancel(session.pk)  # the fake CLI never exits by itself
                return
            time.sleep(0.1)

    thread = threading.Thread(target=paste)
    thread.start()
    session = _run(session, fake_cli(tmp_path, OMP_ANTHROPIC))
    thread.join()
    assert relayed == [checked]
    assert session.outcome == "cancelled"


def test_cancel_kills_the_process(staff, bot, redis, tmp_path, caplog, monkeypatch):
    caplog.set_level(logging.DEBUG)
    session = _session(staff, store="omp/anthropic")
    procs = []
    pid_file = tmp_path / "cli.pid"
    argv = fake_cli(tmp_path, f"import os\nopen({str(pid_file)!r}, 'w').write(str(os.getpid()))\n" + OMP_ANTHROPIC)

    def spawn(ssh):
        procs.append(_relayed(argv))
        return procs[-1]

    def cancel():
        for _ in range(100):
            if f"relogin:{session.pk}:show" in redis.data:  # the worker showed the link
                handoff.cancel(session.pk)
                return
            time.sleep(0.1)

    thread = threading.Thread(target=cancel)
    thread.start()
    runner.run(session.pk, spawn=spawn)
    thread.join()
    session.refresh_from_db()
    assert session.outcome == "cancelled" and session.ended_at
    assert procs[0].proc.poll() is not None  # the relay (ssh's stand-in) is reaped
    cli_pid = int(pid_file.read_text())
    for _ in range(50):  # and the CLI behind the relay's terminal is gone too
        try:
            os.kill(cli_pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.1)
    else:
        pytest.fail("the CLI survived the cancel")
    _no_secrets(session, caplog, redis)


def test_timeout_expires_and_kills(staff, bot, redis, tmp_path):
    session = _session(staff, store="omp/anthropic", expires_at=timezone.now() + timedelta(seconds=2))
    session = _run(session, fake_cli(tmp_path, OMP_ANTHROPIC))
    assert session.outcome == "expired" and session.exit_code is not None and session.exit_code != 0


def test_unreadable_output_fails_closed(staff, bot, redis, tmp_path, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    monkeypatch.setattr(runner, "URL_WAIT", 1)
    session = _session(staff, store="codex-cli")
    session = _run(
        session,
        fake_cli(
            tmp_path, "import time\nprint('Sign in at https://evil.example/device code ABCD-EFGH')\ntime.sleep(30)\n"
        ),
    )
    assert session.outcome == "failed" and session.error == runner.CANT_READ
    assert not redis.data.get(f"relogin:{session.pk}:show")


def test_post_check_failure_fails_the_session(staff, bot, redis, tmp_path):
    bot.module.relogin_post_check = lambda ctx, key, before: {
        "ok": False,
        "state": "expired",
        "detail": "see https://x.test/a",
        "account": "different",
    }
    session = _run(_session(staff, store="codex-cli"), fake_cli(tmp_path, CODEX_DEVICE))
    assert session.outcome == "failed" and session.account == "different"
    assert session.post_check == {"ok": False, "state": "expired", "detail": "see [link]"}


def test_stale_sessions_are_swept(staff, bot, redis):
    session = _session(staff, expires_at=timezone.now() - timedelta(minutes=5))
    assert runner.sweep() == 1
    session.refresh_from_db()
    assert session.outcome == "expired"
