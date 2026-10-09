# Browser control

The `browser` plugin lets a bot drive a real Chrome: open pages, read them,
click, type and take screenshots. The bot doesn't start the browser. It
attaches to a Chrome that a person started with remote debugging, over the
Chrome DevTools Protocol (CDP). Because that Chrome is an ordinary window
on someone's machine:

- a person can sign in to sites for the bot (Google, say) in that window,
  and the bot uses those sign-ins;
- traffic leaves from that machine's IP, so a browser on a home connection
  isn't blocked the way datacenter IPs often are;
- Chrome isn't started by an automation driver, so it doesn't carry the
  automation flags that make sign-in pages refuse it.

The trade-off is that the bot can only browse while that machine and its
Chrome are running.

## Set up

Install the extra where Ergonaut runs (`ergonaut/Makefile` and the
Ergonaut image already include it):

```bash
uv pip install 'django-ergo[browser]'
```

Playwright needs no browser download here; it only connects.

Start Chrome with a profile of its own and the debugging port. Chrome
refuses remote debugging on your everyday profile, and a separate profile
keeps the bot out of your own tabs and accounts anyway:

```bash
google-chrome --user-data-dir="$HOME/.config/ergo-chrome" --remote-debugging-port=9222
```

Chrome binds the port to 127.0.0.1. Leave it that way: the port gives
full control of the browser, with every signed-in account, and has no
authentication.

Add the plugin to bot.yaml and the `browser` skill to the chats that
should have it:

```yaml
chats:
  main:
    skills: [browser]
plugins:
  - name: browser
    cdp_url: http://127.0.0.1:9222
    takeover: the Chrome window on rigel   # where a person signs in for the bot
```

Then sign the profile in to whatever the bot needs, in that window.
The profile keeps its cookies across restarts.

### When Ergonaut runs on another machine

If the Ergonaut host can SSH to the browser machine (as the user Ergonaut
runs as, with keys and no prompts), set `ssh_host` and leave `cdp_url` as
the browser machine sees it:

```yaml
plugins:
  - name: browser
    ssh_host: rigel                 # a name from ~/.ssh/config, or user@host
    cdp_url: http://127.0.0.1:9222  # on rigel
```

The first browser call on a host (or pod) starts an ssh ControlMaster
there that forwards a local port to `cdp_url` (`ssh -fN -M -L`), and later
calls on that host reuse it. Ergonaut runs tool calls in several pods, so
each pod opens its own; none of them has to be kept up by hand. A master
closes when the session stops (that pod's), at the next browser call after
the session ended, or after `idle_minutes` without use. Chrome stays bound
to localhost. Errors from ssh (an unknown host, a refused key) come back
to the bot as the reason the call failed.

The pods need an ssh client, a key the browser machine accepts and a
`known_hosts` entry for it, since ssh runs with `BatchMode=yes`.

Without SSH from the Ergonaut host, bring the port over from the browser
machine instead, with a reverse tunnel that it dials out for:

```bash
ssh -N -o ServerAliveInterval=30 -o ExitOnForwardFailure=yes \
    -R 9222:127.0.0.1:9222 ergonaut-host
```

`cdp_url` is then `http://127.0.0.1:9222` on the Ergonaut host. Use
`autossh` or a systemd user unit (`Restart=always`) to keep it up. A
private network works too; point `cdp_url` at the browser machine's
private IP, not a hostname, because Chrome refuses DevTools requests whose
`Host` header is a name other than `localhost`. Chrome has to listen on
that interface for this, so prefer `ssh_host`. Never expose the port on a
public address.

## How the bot uses it

The bot opens a session with `ergo_browser_start`, which always waits for
the user's approval, and ends it with `ergo_browser_stop`. A session
belongs to the bot, since there is one Chrome. It shows in the chat that
started it as a worker, "Browser on rigel", that checks Chrome answers
every `poll_seconds` and ends the session after `idle_minutes` without a
browser call, closing the tabs it opened. While no session is open the
other browser tools refuse to run. With `launch_command` set, starting a
session runs it (over `ssh_host`) when Chrome doesn't answer; it must
detach, for example
`DISPLAY=:1 setsid google-chrome --user-data-dir=$HOME/.config/ergo-chrome --remote-debugging-port=9222 >/dev/null 2>&1 &`.
`sessions: false` drops the start and stop tools for a bot that should
always have the browser.

| Tool | Does | Approval |
| --- | --- | --- |
| `ergo_browser_start` | open a browser session | always |
| `ergo_browser_stop` | end it, close the tabs it opened | no |
| `ergo_browser_tabs` | list open tabs: id, title, URL | no |
| `ergo_browser_open` | go to a URL (current tab or a new one) | no |
| `ergo_browser_snapshot` | the page as an accessibility tree, with refs like `[ref=e12]` | no |
| `ergo_browser_click` | click a ref or Playwright selector | `approve_actions` |
| `ergo_browser_type` | fill a field, optionally press Enter | `approve_actions` |
| `ergo_browser_press` | press a key or chord | `approve_actions` |
| `ergo_browser_screenshot` | attach a screenshot to the chat | no |

Opening, reading and the actions all return the page's title, URL and new
snapshot, so the bot sees what changed. Each call connects and disconnects;
Chrome and its tabs stay as they are. Refs only resolve against a snapshot
taken on the same connection, so an action takes a fresh snapshot and
resolves the ref against it: if the page changed in between, the ref may
point somewhere else; set `approve_actions: true` to approve each action
too. Each chat's current tab is kept on the session.

When a page needs a person (a sign-in, a two-factor code, a captcha, a
consent screen) the bot's context tells it to stop and ask the user to do
it in the `takeover` place, then snapshot again. It never asks for
passwords in chat.

All options are in the [bot reference](bots.md#browser).
