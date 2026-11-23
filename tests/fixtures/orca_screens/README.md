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
- `codex/busy_working.txt`: this supervised worker's Orca terminal
  `term_408c993c-f24a-4524-b9c3-bb6d7671f993`, 2026-11-23, busy while
  commands were running; installed `codex --version` reported `codex-cli 0.164.0`.
  Unrelated output was omitted. The background-terminal count is part of the
  busy footer, not evidence that the agent is idle.

`synthetic_*` files were written from the CLIs' known wording because no live terminal showed
that state during the survey; replace them with real captures when one turns up.

| CLI | Observed coverage | Synthetic or unobserved coverage |
| --- | --- | --- |
| Codex | Busy footer (2026-11-23); shell return (older capture; version/date not recorded) | Approval, usage limit, retry, resume exit; idle prompt not captured separately |
| omp | Recap and out-of-credits error (older captures; version/date/provider not recorded) | Busy and expired login; approval, retry, and exit not captured |
| Claude | Busy spinner, background work, completed turn, trust prompt/decline, fresh launch | Permission, login, usage limit, retry and quoted error |

The older omp captures do not establish xai-oauth provenance, so they do not
substitute for a supervised xai-oauth probe. Synthetic fixtures are regression
examples, not evidence that a particular CLI version displays those strings.

Fixture inventory: versions and dates not listed above were not recorded;
synthetic entries have no CLI version or terminal source.

| Fixture | Evidence | Version and source |
| --- | --- | --- |
| `claude/busy_background_shells.txt` | Observed | Version not recorded; source listed above |
| `claude/busy_spinner.txt` | Observed | Version not recorded; source listed above |
| `claude/done_turn_finished.txt` | Observed | Version not recorded; source listed above |
| `claude/exited_trust_declined.txt` | Observed | Claude Code 2.1.329; terminal run listed above |
| `claude/synthetic_busy_quotes_401.txt` | Synthetic | No observed CLI version or terminal; authored example |
| `claude/synthetic_error_login.txt` | Synthetic | No observed CLI version or terminal; authored example |
| `claude/synthetic_error_retry_loop.txt` | Synthetic | No observed CLI version or terminal; authored example |
| `claude/synthetic_error_usage_limit.txt` | Synthetic | No observed CLI version or terminal; authored example |
| `claude/synthetic_waiting_permission.txt` | Synthetic | No observed CLI version or terminal; authored example |
| `claude/unknown_fresh_launch.txt` | Observed | Claude Code 2.1.329; terminal run listed above |
| `claude/waiting_trust_dialog.txt` | Observed | Claude Code 2.1.329; terminal run listed above |
| `codex/busy_working.txt` | Observed | Installed codex-cli 0.164.0; worker terminal listed above, 2026-11-23 |
| `codex/exited_shell_prompt.txt` | Observed | Version not recorded; source listed above |
| `codex/synthetic_error_retry_loop.txt` | Synthetic | No observed CLI version or terminal; authored example |
| `codex/synthetic_error_usage_limit.txt` | Synthetic | No observed CLI version or terminal; authored example |
| `codex/synthetic_exited_resume.txt` | Synthetic | No observed CLI version or terminal; authored example |
| `codex/synthetic_waiting_approval.txt` | Synthetic | No observed CLI version or terminal; authored example |
| `omp/done_recap.txt` | Observed | Version not recorded; source listed above |
| `omp/error_out_of_credits.txt` | Observed | Version not recorded; source listed above |
| `omp/synthetic_busy_working.txt` | Synthetic | No observed CLI version or terminal; authored example |
| `omp/synthetic_error_login_expired.txt` | Synthetic | No observed CLI version or terminal; authored example |
