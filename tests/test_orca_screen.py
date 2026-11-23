# ruff: noqa: RUF001 — real screen glyphs
"""orca_screen.classify on real (and a few synthetic) agent terminal screens."""

from pathlib import Path

import pytest

from django_ergo.plugins.orca import agent_blocked
from django_ergo.plugins.orca_screen import BUSY
from django_ergo.plugins.orca_screen import DONE
from django_ergo.plugins.orca_screen import ERROR
from django_ergo.plugins.orca_screen import EXITED
from django_ergo.plugins.orca_screen import STALLED
from django_ergo.plugins.orca_screen import UNKNOWN
from django_ergo.plugins.orca_screen import WAITING
from django_ergo.plugins.orca_screen import Snapshot
from django_ergo.plugins.orca_screen import classify
from django_ergo.plugins.orca_screen import clean_lines
from django_ergo.plugins.orca_screen import fingerprint

SCREENS = Path(__file__).parent / "fixtures" / "orca_screens"
PREFIX = {
    "busy": BUSY,
    "waiting": WAITING,
    "error": ERROR,
    "exited": EXITED,
    "done": DONE,
    "unknown": UNKNOWN,
}
NOW = 1_790_000_000.0


def screen(name: str) -> list[str]:
    return (SCREENS / name).read_text().splitlines()


def fixtures():
    for path in sorted(SCREENS.glob("*/*.txt")):
        stem = path.stem.removeprefix("synthetic_")
        yield pytest.param(
            path.parent.name,
            f"{path.parent.name}/{path.name}",
            PREFIX[stem.split("_", 1)[0]],
            id=f"{path.parent.name}/{path.stem}",
        )


def shell_evidence(lines: list[str]) -> str:
    """#370's rule for a CLI back at the shell, as the watcher feeds it in."""
    reason = agent_blocked(lines, checked_in=True, launched_at=None)
    return reason if "shell prompt" in reason else ""


@pytest.mark.parametrize(("cli", "name", "state"), list(fixtures()))
def test_each_fixture_gets_its_state(cli, name, state):
    lines = screen(name)
    verdict = classify(cli, lines, None, NOW, exited=shell_evidence(lines))
    assert verdict.state == state, (verdict.reason, verdict.evidence)
    if state not in (UNKNOWN,):
        assert verdict.evidence


def test_the_trust_dialog_incident():
    # 2026-11-20 00:29 UTC. The dialog itself is #370's agent_blocked (before
    # check-in); here it's just a prompt.
    dialog = screen("claude/waiting_trust_dialog.txt")
    assert "trust prompt" in agent_blocked(dialog, checked_in=False, launched_at=None)
    assert classify("claude", dialog, None, NOW).state == WAITING
    # Orca's Enter picked "No, exit": Claude's own exit message is enough.
    gone = classify("claude", screen("claude/exited_trust_declined.txt"), None, NOW)
    assert (gone.state, gone.fatal) == (EXITED, True)
    assert (
        gone.evidence == "Run claude again and choose Yes, or run it in another folder."
    )


def test_caller_exit_evidence_wins():
    lines = screen("codex/exited_shell_prompt.txt")
    assert classify("codex", lines, None, NOW).state != EXITED
    verdict = classify("codex", lines, None, NOW, exited=shell_evidence(lines))
    assert (verdict.state, verdict.fatal) == (EXITED, True)


def test_an_agent_quoting_errors_while_working_is_busy():
    verdict = classify(
        "claude", screen("claude/synthetic_busy_quotes_401.txt"), None, NOW
    )
    assert verdict.state == BUSY


def test_generic_error_text_is_reported_but_not_fatal():
    lines = [
        "● Pushing the branch.",
        "  ⎿  remote: invalid_grant: the refresh token was revoked",
        "❯",
    ]
    verdict = classify("claude", lines, None, NOW)
    assert (verdict.state, verdict.fatal) == (ERROR, False)


@pytest.mark.parametrize(
    ("name", "fatal"),
    [
        ("claude/synthetic_error_usage_limit.txt", True),
        ("claude/synthetic_error_login.txt", True),
        ("claude/synthetic_error_retry_loop.txt", False),
        ("codex/synthetic_error_usage_limit.txt", True),
        ("codex/synthetic_error_retry_loop.txt", False),
        ("omp/error_out_of_credits.txt", True),
        ("omp/synthetic_error_login_expired.txt", True),
    ],
)
def test_cli_errors_are_fatal_retry_loops_are_not(name, fatal):
    verdict = classify(name.split("/")[0], screen(name), None, NOW)
    assert (verdict.state, verdict.fatal) == (ERROR, fatal)


def test_an_early_retry_is_still_busy():
    lines = [
        "  ⎿  API Error (529 Overloaded) · Retrying in 2 seconds… (attempt 1/10)",
        "✶ Skedaddling… (0m 9s · ↓ 12 tokens)",
        "  ⏵⏵ bypass permissions on · esc to interrupt",
    ]
    assert classify("claude", lines, None, NOW).state == BUSY


def test_unchanged_screen_turns_stalled_after_the_threshold():
    lines = screen("claude/done_turn_finished.txt")
    first = classify("claude", lines, None, NOW, stall_after=480)
    assert first.state == DONE
    soon = classify("claude", lines, first.snapshot, NOW + 300, stall_after=480)
    assert (soon.state, soon.unchanged_for) == (DONE, 300)
    later = classify("claude", lines, soon.snapshot, NOW + 500, stall_after=480)
    assert later.state == STALLED
    assert later.snapshot.since == NOW
    # New output resets the clock.
    moved = classify("claude", [*lines, "● More"], later.snapshot, NOW + 600)
    assert moved.snapshot.since == NOW + 600
    assert moved.state != STALLED


def test_a_frozen_spinner_is_stalled_but_background_work_is_not():
    spinner = screen("claude/busy_spinner.txt")
    first = classify("claude", spinner, None, NOW)
    assert first.state == BUSY
    frozen = classify("claude", spinner, first.snapshot, NOW + 900, stall_after=480)
    assert frozen.state == STALLED
    shells = screen("claude/busy_background_shells.txt")
    first = classify("claude", shells, None, NOW)
    waiting = classify("claude", shells, first.snapshot, NOW + 3600, stall_after=480)
    assert waiting.state == BUSY
    assert "1 shell" in waiting.evidence


def test_codex_advancing_busy_footer_resets_stall_timer():
    lines = screen("codex/busy_working.txt")
    first = classify("codex", lines, None, NOW)
    frozen = classify("codex", lines, first.snapshot, NOW + 480)
    assert frozen.state == STALLED
    advanced = [line.replace("1m 08s", "1m 09s") for line in lines]
    moving = classify("codex", advanced, frozen.snapshot, NOW + 481)
    assert (moving.state, moving.unchanged_for) == (BUSY, 0)


def test_unknown_cli_still_sees_shell_return_and_prompts():
    assert classify("", ["Press Enter to continue"], None, NOW).state == WAITING
    assert classify("aider", [], None, NOW).state == UNKNOWN


def test_snapshot_round_trips_and_ignores_colors():
    snap = Snapshot(fingerprint="abc", since=NOW, state=BUSY)
    assert Snapshot.from_dict(snap.to_dict()) == snap
    assert Snapshot.from_dict(None) is None
    assert clean_lines(["\x1b[1;32m✓ built\x1b[0m", "  "]) == ["✓ built"]
    assert fingerprint(clean_lines(["\x1b[1mhi"])) == fingerprint(["hi"])


def test_omp_observed_spinner_advances_freezes_and_returns_to_idle():
    busy = screen("omp/busy_spinner.txt")
    advanced = screen("omp/busy_spinner_advanced.txt")
    idle = screen("omp/done_idle_prompt.txt")
    first = classify("omp", busy, None, NOW)
    assert first.state == BUSY
    frozen = classify("omp", busy, first.snapshot, NOW + 480)
    assert frozen.state == STALLED
    moving = classify("omp", advanced, frozen.snapshot, NOW + 481)
    assert (moving.state, moving.unchanged_for) == (BUSY, 0)
    assert classify("omp", busy + idle, moving.snapshot, NOW + 482).state == DONE
    assert (
        classify("omp", ["⠸ Thinking / not an omp header"], None, NOW).state == UNKNOWN
    )
