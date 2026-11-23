# ruff: noqa: RUF001, RUF003 — the screens' own glyphs (❯ ›) are the signatures
"""What a coding agent's terminal screen says about it: busy, waiting, stalled, dead.

``classify(cli, tail, prev, now)`` reads the last lines of an Orca agent terminal
(``terminal read``) for Claude Code (``claude``), Codex (``codex``) or ``omp`` and
labels it:

- ``busy``: working (a spinner with "esc to interrupt", a running tool), or idle
  while its own background shells or monitors run ("1 shell still running").
- ``waiting_input``: blocked on a prompt (a permission or approval question,
  "Press Enter", y/n). The folder-trust dialog before check-in is
  ``orca.agent_blocked``'s.
- ``error_loop``: a rate or usage limit, a login or auth failure, out of credits,
  or an API error it keeps retrying.
- ``exited``: the CLI is gone: it printed its exit message, or the caller saw the
  shell prompt back (``exited``; ``orca.agent_blocked`` owns that rule).
- ``done``: its turn finished and it sits at its own prompt.
- ``stalled``: the screen hasn't changed for ``stall_after`` seconds and it isn't
  idle with background work (a frozen spinner counts as stalled).
- ``unknown``: nothing recognisable (an empty screen, a CLI still starting).

Pure Python, no I/O: the signatures are regex tables gathered from real devbox
screens (see ``tests/fixtures/orca_screens``). ``Snapshot`` carries what the
previous check saw, so "unchanged since" survives between polls.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import asdict
from dataclasses import dataclass

BUSY = "busy"
WAITING = "waiting_input"
STALLED = "stalled"
EXITED = "exited"
ERROR = "error_loop"
DONE = "done"
UNKNOWN = "unknown"
STATES = (BUSY, WAITING, STALLED, EXITED, ERROR, DONE, UNKNOWN)

# The bottom of the screen is where the CLI's state is; older lines are scrollback.
BOTTOM_LINES = 14
FINGERPRINT_LINES = 40
# A retrying API error is a loop from this attempt on.
RETRY_LOOP_ATTEMPT = 3
# Generic error text only counts this close to the bottom (the agent's own prose
# can quote errors; a CLI's error sits right above its prompt).
WEAK_ERROR_LINES = 6

ANSI = re.compile(
    r"\x1b(?:\[[0-9;?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)|[()#][0-9A-Za-z]|[@-Z\\-_])"
)


def _rx(*patterns: str) -> re.Pattern:
    return re.compile("|".join(f"(?:{p})" for p in patterns), re.I | re.M)


@dataclass(frozen=True)
class Signatures:
    busy: re.Pattern
    background: re.Pattern  # idle at its prompt while its own shells/monitors run
    waiting: re.Pattern
    error: re.Pattern
    retry: re.Pattern  # group "attempt" is the retry number
    exit: re.Pattern
    done: re.Pattern
    chrome: re.Pattern  # anything proving the CLI drew its UI


# Shared by every CLI: prompts any terminal program can block on.
COMMON_WAITING = (
    r"press enter to (?:continue|confirm)",
    r"\((?:y/n|yes/no)\)\s*\??\s*$",
    r"\[(?:y/n|Y/n|y/N)\]\s*\??\s*$",
    r"enter to confirm\s*·\s*esc to (?:cancel|exit)",
    r"do you want to (?:proceed|continue|make this edit|create|allow)",
)
# Generic auth/credit errors: reported, never fatal on their own.
COMMON_ERROR = (
    r"\b(?:401|403)\b.{0,40}(?:unauthori[sz]ed|forbidden|invalid|expired|credits)",
    r"(?:oauth|access|refresh)\s+token (?:has )?(?:expired|could not be refreshed|is invalid|revoked)",
    r"invalid[_ ]grant",
    r"(?:not|no longer) (?:logged|signed) in",
    r"please (?:run /login|log ?in|sign in)",
    r"invalid api key",
    r"(?:credit balance is too low|run out of credits|insufficient[_ ](?:quota|credits))",
    r"spending[- ]limit",
    r"retry failed after \d+ attempts?",
)

CLAUDE = Signatures(
    # "✶ Skedaddling… (13m 48s · ↓ 31.9k tokens)", footer "· esc to interrupt".
    busy=_rx(
        r"esc to interrupt",
        r"^\s*\S\s+\w+…\s+\(\d+[hms][^)]*(?:tokens|esc)",
        r"^● Running \d+ (?:shell )?commands?…",
    ),
    # "✻ Brewed for 6m 12s · done 4:36 PM · 1 shell, 2 monitors still running",
    # footer "⏵⏵ bypass permissions on · 1 shell · ↓ to manage".
    background=_rx(
        r"\d+ (?:shells?|monitors?|agents?|tasks?)(?:, \d+ \w+)* still running",
        r"⏵⏵.*·\s*\d+ (?:shells?|monitors?|background tasks?)\b",
    ),
    waiting=_rx(
        r"^\s*❯\s*\d+\.\s+Yes\b",  # a numbered permission menu
        r"No, and tell Claude what to do differently",
        r"Esc to cancel · Tab to amend",
    ),
    error=_rx(
        r"Claude (?:AI )?usage limit reached",
        r"You've hit your (?:\w+ )?limit",
        r"(?:5-hour|weekly|session|Opus) limit reached",
        r"API Error:? \(?(?:401|403)",
        r"OAuth token (?:has )?expired",
        r"Invalid API key · (?:Please )?run /login",
        r"Credit balance is too low",
    ),
    # "⎿ API Error (529 Overloaded) · Retrying in 8 seconds… (attempt 4/10)"
    retry=re.compile(r"Retrying in \d+\s*\w*… \(attempt (?P<attempt>\d+)/\d+\)", re.I),
    exit=_rx(
        r"Claude Code didn't start because this folder isn't trusted",
        r"Run claude again and choose Yes",
        r"^Resume this session with:?\s*$",
        r"^claude --resume [0-9a-f-]{8,}",
    ),
    # "✻ Sautéed for 3s · done 3:31 PM" with the "❯" input box and a quiet footer.
    done=_rx(
        r"^\s*\S\s+\w+ for \d+[hms][\w ]*(?:· done [\d:]+ ?[AP]M)?\s*$",
        r"· done \d+:\d+ ?[AP]M",
    ),
    chrome=_rx(
        r"⏵⏵", r"Claude Code v\d", r"^\s*▐▛███▜▌|▐▛███▛█", r"shift\+tab to cycle"
    ),
)

CODEX = Signatures(
    # "• Working (12s • esc to interrupt)"
    busy=_rx(r"esc to interrupt", r"^\W*Working \(\d+[hms]"),
    background=_rx(r"\d+ background (?:terminals?|shells?|tasks?) running"),
    waiting=_rx(
        r"Would you like to (?:run the following command|make the following edits|allow)",
        r"Allow (?:command|Codex)",
        r"Press enter to confirm or esc to cancel",
        r"^\s*[›>▌]\s*\d+\.\s+Yes\b",
    ),
    error=_rx(
        r"You've hit your usage limit",
        r"usage limit (?:reached|exceeded)",
        r"Token data is not available",
        r"Your access token could not be refreshed",
        r"stream disconnected before completion",
        r"^\W*(?:401 )?Unauthorized\b.*(?:codex login|log in)",
    ),
    # "⚠ stream error: ...; retrying 3/5 in 1.2s…", "Reconnecting... 2/5"
    retry=re.compile(r"(?:retrying|Reconnecting\.*)\s*(?P<attempt>\d+)/\d+", re.I),
    exit=_rx(r"To continue this session, run codex resume", r"^Token usage: total="),
    done=_rx(r"^\s*›(?:\s.*)?$"),
    chrome=_rx(r"OpenAI Codex", r"^\s*›", r"esc to interrupt", r"\d+% context left"),
)

OMP_HEADER = _rx(r"^\s*╭──\s*(?:π|[⠁-⣿]\s+[^/]+)\s*/\s*⬢")
OMP_BUSY_HEADER = _rx(r"^\s*╭──\s*[⠁-⣿]\s+[^/]+\s*/\s*⬢")

OMP = Signatures(
    busy=_rx(
        r"esc to (?:interrupt|cancel)",
        r"^\W*(?:Working|Thinking|Running)\b.*\(\d+[hms]",
    ),
    background=_rx(r"\d+ (?:still running|background (?:jobs?|tasks?) running)"),
    waiting=_rx(r"Approve\?|Allow this (?:command|tool)"),
    # "Error: 403 You have run out of credits or need a Grok subscription..."
    # "Error: Retry failed after 1 attempts: Provider requested 538984731ms wait,
    #  exceeds retry.maxDelayMs (300000ms)."
    error=_rx(
        r"^\s*Error: (?:\d{3}\b|Retry failed)",
        r"exceeds retry\.maxDelayMs",
        r"type=personal-team-blocked",
        r"(?:login|session|credentials?) (?:has )?expired",
        r"invalid[_ ]grant",
    ),
    retry=re.compile(
        r"retry(?:ing)? (?:attempt )?(?P<attempt>\d+)(?:/\d+| of \d+)", re.I
    ),
    exit=_rx(r"^\s*Goodbye[.!]?\s*$"),
    # The turn's "※ recap: ..." summary sits right above the header box.
    done=_rx(r"^\s*※ recap:", r"^\s*╭──\s*π\s*/\s*⬢"),
    # "╭── π / ◑ Grok 4.6 👁 / 📁 …vbox / ⑂ main *3 ?1 ──9%──┃──500K──╮"
    chrome=_rx(r"╭── π / ", r"^\s*π / "),
)

GENERIC = Signatures(
    busy=_rx(r"esc to interrupt"),
    background=_rx(r"(?!x)x"),
    waiting=_rx(r"(?!x)x"),
    error=_rx(r"(?!x)x"),
    retry=re.compile(r"(?!x)x(?P<attempt>\d)"),
    exit=_rx(r"(?!x)x"),
    done=_rx(r"(?!x)x"),
    chrome=_rx(r"(?!x)x"),
)

TABLES = {"claude": CLAUDE, "codex": CODEX, "omp": OMP}
COMMON_ERROR_RX = _rx(*COMMON_ERROR)
COMMON_WAITING_RX = _rx(*COMMON_WAITING)


@dataclass
class Snapshot:
    """What one check saw, kept between checks (``ctx.state``)."""

    fingerprint: str = ""
    since: float = 0.0  # when the screen last changed
    state: str = UNKNOWN

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict | None) -> Snapshot | None:
        if not value:
            return None
        return cls(
            fingerprint=str(value.get("fingerprint") or ""),
            since=float(value.get("since") or 0.0),
            state=str(value.get("state") or UNKNOWN),
        )


@dataclass
class Verdict:
    state: str
    reason: str
    evidence: str  # the screen line that decided it ("" when none did)
    snapshot: Snapshot
    unchanged_for: float = 0.0  # seconds since the screen last changed
    fatal: bool = False  # the CLI can't go on by itself (exited, a CLI's own error)


def clean_lines(tail) -> list[str]:
    """The screen's non-blank lines, colors stripped."""
    lines = (ANSI.sub("", str(line)).rstrip() for line in tail or [])
    return [line for line in lines if line.strip()]


def fingerprint(lines: list[str]) -> str:
    text = "\n".join(lines[-FINGERPRINT_LINES:])
    return hashlib.sha1(text.encode()).hexdigest()  # noqa: S324 — change check only


def _search(pattern: re.Pattern, lines: list[str], *, first: bool = False) -> str:
    """The newest line matching ``pattern`` (the oldest with ``first``: a
    prompt's question sits above its options)."""
    for line in lines if first else reversed(lines):
        if pattern.search(line):
            return line.strip()
    return ""


def classify(  # noqa: C901, PLR0911, PLR0912, PLR0913 — one table-driven decision
    cli: str,
    tail,
    prev: Snapshot | None = None,
    now: float = 0.0,
    *,
    stall_after: float = 480.0,
    exited: str = "",
) -> Verdict:
    """Label an agent terminal's screen (``tail``: its lines, oldest first).

    ``exited`` is the caller's evidence that the CLI is gone (in the watcher,
    ``orca.agent_blocked``'s shell-prompt reason): the shell prompt and trust
    dialog rules live there, not here."""
    sig = TABLES.get((cli or "").lower(), GENERIC)
    lines = clean_lines(tail)
    bottom = lines[-BOTTOM_LINES:]
    mark = fingerprint(lines)
    since = prev.since if prev and prev.fingerprint == mark and prev.since else now
    unchanged = max(0.0, now - since)

    def verdict(
        state: str, reason: str, evidence: str = "", *, fatal: bool = False
    ) -> Verdict:
        snap = Snapshot(fingerprint=mark, since=since, state=state)
        fatal = fatal or state == EXITED
        return Verdict(state, reason, evidence[:300], snap, unchanged, fatal)

    if not lines:
        return verdict(UNKNOWN, "empty screen")

    # The CLI said goodbye, or the caller saw its shell come back.
    if exited:
        return verdict(EXITED, "shell prompt returned", exited)
    if said := _search(sig.exit, bottom):
        return verdict(EXITED, f"{cli or 'agent'} exited", said)

    retry = None
    for line in reversed(bottom):
        if found := sig.retry.search(line):
            retry = (int(found.group("attempt")), line.strip())
            break
    if retry and retry[0] >= RETRY_LOOP_ATTEMPT:
        return verdict(ERROR, f"retrying (attempt {retry[0]})", retry[1])

    # The CLI's own question names what it asks; the generic "Enter to confirm" is a fallback.
    if asking := _search(sig.waiting, bottom, first=True) or _search(
        COMMON_WAITING_RX, bottom, first=True
    ):
        return verdict(WAITING, "waiting on a prompt", asking)

    # Working wins over error text: an agent may quote "401 Unauthorized" itself.
    # omp redraws its header as the spinner advances and restores π at idle.
    # Only the newest header describes the current state, not scrollback.
    omp_header = _search(OMP_HEADER, bottom) if sig is OMP else ""
    busy_lines = (
        [line for line in bottom if not OMP_HEADER.search(line)]
        if sig is OMP
        else bottom
    )
    working = _search(sig.busy, busy_lines)
    if omp_header and OMP_BUSY_HEADER.search(omp_header):
        working = omp_header
    if working:
        if unchanged >= stall_after:
            return verdict(STALLED, "busy indicator frozen", working)
        return verdict(BUSY, "working", working)
    if background := _search(sig.background, bottom):
        return verdict(BUSY, "waiting on its background work", background)

    if fatal := _search(sig.error, bottom):
        return verdict(ERROR, "CLI error", fatal, fatal=True)
    if weak := _search(COMMON_ERROR_RX, lines[-WEAK_ERROR_LINES:]):
        return verdict(ERROR, "error text above its prompt", weak)
    if retry:
        return verdict(BUSY, f"retrying (attempt {retry[0]})", retry[1])

    if unchanged >= stall_after:
        return verdict(STALLED, "no output change", lines[-1].strip())
    if finished := _search(sig.done, bottom):
        return verdict(DONE, "turn finished, at its prompt", finished)
    return verdict(UNKNOWN, "no known signature", lines[-1].strip())
