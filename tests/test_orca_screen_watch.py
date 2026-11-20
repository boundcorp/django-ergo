"""OrcaPlugin.watch_screen: what the watcher does with each screen state."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from django_ergo.plugins.orca import OrcaPlugin

SCREENS = Path(__file__).parent / "fixtures" / "orca_screens"
SHOWN = {"worker": {"agent_terminal_handle": "term_agent"}}


class FakePlugin:
    stall_minutes = 8

    def __init__(self, screen: list[str]):
        self.screen = screen
        self.ended = []
        self.reads = 0

    def cli_json(self, args):
        assert args[:2] == ["terminal", "read"]
        self.reads += 1
        return {"terminal": {"tail": list(self.screen)}}

    def end_worker(self, dispatch, status):
        self.ended.append((dispatch, status))
        return "closed"

    watch_screen = OrcaPlugin.watch_screen


def handle(agent="claude", launched_at=None):
    return {"dispatch": "ctx_1", "agent": agent, "launched_at": launched_at}


def screen(name):
    return (SCREENS / name).read_text().splitlines()


@pytest.fixture
def clock(monkeypatch):
    now = {"t": 1_790_000_000.0}
    monkeypatch.setattr("django_ergo.plugins.orca.time.time", lambda: now["t"])
    return now


def test_trust_dialog_exit_stops_the_worker_with_the_evidence(clock):
    # The 2026-11-20 00:29 UTC incident, if #370's shell-prompt check missed it
    # (the exit message is enough on its own).
    plugin = FakePlugin(screen("claude/exited_trust_declined.txt")[:-2])
    ctx = SimpleNamespace(state={})
    with pytest.raises(RuntimeError) as failed:
        plugin.watch_screen(ctx, handle(launched_at=clock["t"] - 20), SHOWN)
    assert plugin.ended == [("ctx_1", "running")]
    assert "exited" in str(failed.value)
    assert "Run claude again and choose Yes" in str(failed.value)


@pytest.mark.parametrize(
    ("agent", "name"),
    [
        ("omp", "omp/error_out_of_credits.txt"),
        ("codex", "codex/synthetic_error_usage_limit.txt"),
        ("claude", "claude/synthetic_error_login.txt"),
    ],
)
def test_cli_errors_stop_the_worker(clock, agent, name):
    plugin = FakePlugin(screen(name))
    with pytest.raises(RuntimeError, match="error loop"):
        plugin.watch_screen(SimpleNamespace(state={}), handle(agent), SHOWN)
    assert plugin.ended


def test_busy_and_background_work_stay_quiet(clock):
    for name in ("claude/busy_spinner.txt", "claude/busy_background_shells.txt"):
        plugin = FakePlugin(screen(name))
        ctx = SimpleNamespace(state={})
        assert plugin.watch_screen(ctx, handle(), SHOWN) == []
        assert ctx.state["screen"]["state"] == "busy"
        assert plugin.ended == []


def test_a_waiting_prompt_is_escalated_once(clock):
    plugin = FakePlugin(screen("codex/synthetic_waiting_approval.txt"))
    ctx = SimpleNamespace(state={})
    first = plugin.watch_screen(ctx, handle("codex"), SHOWN)
    assert len(first) == 1
    question = first[0]
    assert question.kind == "escalation"
    assert question.reply_supported is False
    assert "Would you like to run the following command?" in question.body
    assert "term_agent" in question.body
    clock["t"] += 120
    again = plugin.watch_screen(ctx, handle("codex"), SHOWN)
    # Same id, so watch() (which dedupes on it) tells the chat only once.
    assert [q.id for q in again] == [question.id]
    assert plugin.ended == []


def test_a_retry_loop_keeps_one_id_while_its_counter_moves(clock):
    lines = screen("claude/synthetic_error_retry_loop.txt")
    plugin = FakePlugin(lines)
    ctx = SimpleNamespace(state={})
    first = plugin.watch_screen(ctx, handle(), SHOWN)
    plugin.screen = [line.replace("attempt 5/10", "attempt 6/10") for line in lines]
    second = plugin.watch_screen(ctx, handle(), SHOWN)
    assert first[0].id == second[0].id
    assert plugin.ended == []


def test_stall_is_reported_once_after_eight_minutes(clock):
    plugin = FakePlugin(screen("claude/done_turn_finished.txt"))
    ctx = SimpleNamespace(state={})
    assert plugin.watch_screen(ctx, handle(), SHOWN) == []
    clock["t"] += 6 * 60
    assert plugin.watch_screen(ctx, handle(), SHOWN) == []
    clock["t"] += 3 * 60
    stalled = plugin.watch_screen(ctx, handle(), SHOWN)
    assert [q.subject for q in stalled] == ["no new output for 9 minutes"]
    clock["t"] += 2 * 60
    assert [q.id for q in plugin.watch_screen(ctx, handle(), SHOWN)] == [stalled[0].id]
    # New output, then a new freeze: a new report.
    plugin.screen = [*plugin.screen, "● Still here"]
    assert plugin.watch_screen(ctx, handle(), SHOWN) == []
    clock["t"] += 10 * 60
    later = plugin.watch_screen(ctx, handle(), SHOWN)
    assert later[0].id != stalled[0].id


def test_no_terminal_or_unreadable_terminal_changes_nothing(clock):
    plugin = FakePlugin([])
    ctx = SimpleNamespace(state={})
    assert plugin.watch_screen(ctx, handle(), {"worker": {}}) == []
    assert plugin.reads == 0

    def boom(args):
        msg = "terminal_handle_stale"
        raise ValueError(msg)

    plugin.cli_json = boom
    assert plugin.watch_screen(ctx, handle(), SHOWN) == []
    assert ctx.state == {}
