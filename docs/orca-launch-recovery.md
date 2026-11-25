# Orca launch journal and recovery

`ergo_agent_start` and `orca_start_worker` accept `launch_key`. Choose a stable,
explicit key before a new Orca job and retain the exact original arguments.
A retry with that same key in the same chat and bot cannot call the external
launch again. Distinct explicit keys intentionally permit identical jobs; there
is no content-based deduplication. Omitting the key creates a fresh request with
a generated key, returned on success or in a recovery error. Never omit/change
the key when retrying a failed request.

The private `AgentLaunch` journal commits before Orca effects. It stores the
original brief and request hash, owner/session, authenticated message reference
when supplied, admission scope, selected routing receipt, and identities as they
become known. A second hash records the exact external brief including appended
authority references and checkpoint guidance. Scoped worktree creation records
its generated name before calling Orca. The row is not exposed through a public
view or registered in admin; access requires existing session/tool authorization
or trusted database operator access. No capability tokens, credentials, raw CLI
output or unrelated native sessions are saved in the journal.

A returned dispatch is persisted before watcher registration. A watcher and its
journal link commit in one transaction under a row lock. If registration fails,
the original dispatch is retained; no speculative stop is issued. Retry the same
key and exact original arguments in the same authorized chat to register a
watcher for that dispatch. Repeated recovery returns the existing watcher. If a
notification callback failed after commit, recovery requeues the existing worker.
Recovery does not reroute, create a worktree/task, or call `worker-start`.

Owner, session, original request, manager environment and repository scope remain
bound to the request hash. Recovery rechecks manager availability and any
supplied authenticated authority message in the original chat. Changed arguments
or admission scope reject the key. An authenticated authority reference proves
message provenance only; it does not independently evaluate semantic permission
or grant new authority. Existing tool approval and admission rules still apply.

## Operator procedure for an uncertain launch

Do not send a replacement start, clear the journal, or assume a CLI error means
nothing happened. `launching` after a crash is also fenced. A CLI with no upstream
request-recovery API cannot prove that repeating a launch is safe.

1. Obtain the launch ID and key from the error. In the deployment's existing
   authorized Django shell, inspect only this journal using its ID **and** original
   session and bot. For example (replace placeholders; this is a read):

   ```python
   from django_ergo.conversation.models import AgentLaunch
   row = AgentLaunch.objects.get(pk=LAUNCH_ID, session_id=SESSION_ID, bot_name=BOT_NAME)
   print(row.status, row.spec_hash, row.evidence, row.handle)
   ```

2. Use the configured Orca environment, original Run and mailbox. The installed
   CLI supports these read-only commands (replace placeholders):

   ```sh
   orca worktree show --worktree WORKTREE --environment ENVIRONMENT --json
   orca orchestration task-list --run RUN --from MAILBOX --environment ENVIRONMENT --json
   orca orchestration worker-show --dispatch DISPATCH --environment ENVIRONMENT --json
   ```

   If only worktree creation may have completed, use the exact generated name and
   repository in `row.evidence` to inspect the worktree. Never guess a dispatch
   from title, brief similarity or timing. A missing task on one page does not
   establish absence. Newer CLIs may require `task-list --full`; launch verification
   tries that and falls back only on the specific older-CLI unknown-flag error.
   Missing/truncated specs or a task absent from the returned page fail closed.

3. If `row.handle` already contains a dispatch, replay the original tool request
   with its original key to recover the watcher. If the dispatch is unknown,
   preserve `recovery_needed` (or interrupted `launching`) while reconciling the
   exact Run/task with Orca operator evidence. This implementation intentionally
   has no tool to attach a guessed dispatch or reset an uncertain launch for
   retry. Escalate an unresolved identity to the Orca operator; a supported,
   authenticated upstream receipt is needed before extending automated recovery.

This provides **no automatic duplicate retry for one key** and recoverable known
dispatches. It does **not** guarantee exactly-once external execution: Orca may
have launched before losing its response, and there is a crash window before
that response can be committed locally. No CLI installation/upgrade/restart or
invented request API is required. Full stored-spec equality proves storage
integrity, not authenticated agent acceptance or delivery into agent context.
