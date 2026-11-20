# Orca agent screens

Terminal tails (`orca terminal read`, ANSI stripped) for `orca_screen.classify`.
File names are `<state>_<what>.txt`; the state is what `classify` must return
(for a bare shell prompt, given `orca.agent_blocked`'s evidence as `exited`).

Real captures from devbox Orca terminals (trimmed, paths kept):

- `claude/busy_spinner.txt`: worker ctx_0f2c4b0bf0d3 (infra-octo/llm-enable-rigel-slot), 2026-11-20.
- `claude/busy_background_shells.txt`: ctx_13b513c7faef (ergo-bots/ficsit-path-analysis).
- `claude/done_turn_finished.txt`: ctx_e8c92b7d666e.
- `claude/waiting_trust_dialog.txt`, `claude/exited_trust_declined.txt`: Claude Code 2.1.329 run in an
  untrusted folder (the text of the 2026-11-19/20 incidents; path set to the incident worktree).
- `claude/unknown_fresh_launch.txt`: Claude Code 2.1.329 just started in a trusted folder.
- `omp/error_out_of_credits.txt`: omp Pi terminal on boundcorp/devbox (Grok spending limit).
- `omp/done_recap.txt`: ctx_7f5cde3be30b.
- `codex/exited_shell_prompt.txt`: codex worker on holyword-ios-android-test-1102 back at the shell.

`synthetic_*` files were written from the CLIs' known wording because no live terminal showed
that state during the survey; replace them with real captures when one turns up.
