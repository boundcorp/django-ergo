# Re-login from the dashboard

When a CLI login that workers use breaks (an omp provider, the Claude CLI, the Codex CLI), a
staff user can redo it from Ergonaut without ssh. The page is at
`/relogin/?bot=<bot>&store=<store key>`. A bot links to it from its dashboard, and from chat it
only sends that link. The design is in ergo-bots `docs/devbox-relogin-design.md`.

## What a bot declares

One of the bot's tool modules defines:

```python
RELOGIN_STORES = {
    "omp/anthropic": {
        "label": "Anthropic login in omp",
        "command": "omp login anthropic",   # must be on Ergonaut's allow-list, exactly
        "shape": "paste_redirect",           # device_code | paste_code | paste_redirect
        "url_hosts": ["claude.ai"],          # sign-in hosts the shown link may point at
        "max_seconds": 300,                  # the provider's limit if under 10 minutes
        "port": 54545,                       # the local callback port, for paste_redirect
    },
}
RELOGIN_SSH = {"host": "...", "port": 2222, "user": "dev",
               "identity": "~/.ssh/id_ed25519", "host_key": "~/.ssh/host_keys/ssh_host_ed25519_key.pub"}

def relogin_identity(ctx, key) -> str | None: ...       # opaque hash of the signed-in account
def relogin_post_check(ctx, key, before) -> dict: ...   # {"ok", "state", "detail", "account"}
```

`DEVBOX_SSH_HOST`, if set, replaces `RELOGIN_SSH["host"]`.

Ergonaut only runs these commands, with exactly this argv
(`ergonaut/apps/relogin/stores.py` `COMMANDS`): `omp login anthropic`,
`omp login openai-codex-device`, `omp login xai-oauth`, `claude auth login --claudeai`,
`codex login --device-auth`. Other entries are not offered. Nothing from a request reaches
the command line. Shown links must be on `claude.ai`, `claude.com`, `auth.openai.com`,
`auth.x.ai` or `accounts.x.ai` (or a subdomain), and on the bot's own `url_hosts` too.

## How a session runs

1. **Start** (POST, CSRF, staff only). This creates a `ReloginSession` bound to the user and the
   store, then queues Celery task `ergonaut.relogin` with the session id only. Only one session
   can be active per store, and one per callback port; database constraints enforce both. A
   session lasts the store's `max_seconds`, 10 minutes at most.
2. **The worker** claims the session (a repeated task does nothing), then opens `ssh -T` to the
   login host. It uses `-F /dev/null`, BatchMode, a known_hosts file that pins `host_key`, and
   no agent or port forwarding. No remote tty: devbox's login shell would attach a tty session
   to the shared tmux. Instead a small fixed Python relay (`runner.RELAY`) on the host runs
   the command in its own 2000-column PTY, under `env -i` with only `HOME`, `USER`, `PATH`,
   `TERM`, `NO_COLOR`, `FORCE_HYPERLINK=0` and `BROWSER=false`. The relay kills the CLI's
   process group when ssh's stdin closes, or at its own deadline (the session's end plus
   30 s), so a lost connection can't leave a login running.
   The output stays in memory (64 KB at most). Then:
   - The worker reads the sign-in link from the output, and the code too for a device flow. If
     either isn't there within 45 s, or a `paste_redirect` link doesn't redirect to
     `http://localhost:<port>/callback`, the session fails closed and the page shows the
     command to run by hand.
   - The link and code go to Redis key `relogin:<id>:show` until the session ends. The page
     polls `/relogin/<id>/status`, which returns them only to the session's user while the
     session waits.
3. **Paste** (POST, CSRF, staff only, one per session):
   - **paste_code:** one line of code characters. If the link carried a `state`, the code must
     be `<code>#<state>` with that state.
   - **paste_redirect:** the URL must be `http`, on `localhost` or `127.0.0.1`, on the store's
     port, with path `/callback`, one `code`, and a `state` equal to the link's. Ergonaut
     rebuilds it as `http://127.0.0.1:<port>/callback?…`.
   - The checked value goes to `relogin:<id>:input` (TTL 60 s), and the worker reads and
     deletes it. It types a code into the PTY. A redirect URL it passes to
     `curl -sS --max-time 10 -K -` on the login host, on stdin, never in argv.
4. **Cancel**, a timeout, or a provider expiry ("expires in 15 minutes") ends the process:
   Ctrl-C, then SIGTERM to ssh (the remote CLI gets SIGHUP), then SIGKILL.
5. **Post-check** runs on every exit: `relogin_post_check`, with the identity hash taken
   before the login. It says whether the store is healthy, and whether the account is `same`
   or `different`. The account id itself never leaves the bot.

The audit row keeps the store, user, start and end times, outcome (`ok`, `failed`, `expired`,
`cancelled`), CLI exit code, the post-check result and same/different account. It never keeps
the sign-in link, codes, pasted values or CLI output. Logs carry only the session id and our
own wording. The user's main chat gets one event message, "Re-login of <store> completed /
failed", with no link. A session no worker picked up is marked `expired` 90 s after its end time, and a claimed
one whose worker died 15 minutes after it.

Re-login needs Redis (`REDIS_URL`) and a Celery broker. Without them the page says so and
starts nothing.
