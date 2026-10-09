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

Bring the port to the Ergonaut host over SSH, from the machine running
Chrome. With a reverse tunnel, the browser machine dials out, so it needs
no open ports:

```bash
ssh -N -o ServerAliveInterval=30 -o ExitOnForwardFailure=yes \
    -R 9222:127.0.0.1:9222 ergonaut-host
```

`cdp_url` stays `http://127.0.0.1:9222` on the Ergonaut host. Use
`autossh` or a systemd user unit (`Restart=always`) for both Chrome and
the tunnel so they come back after a reboot or a dropped connection. A
private network such as Tailscale works too; point `cdp_url` at the
browser machine's private IP, not a hostname, because Chrome refuses
DevTools requests whose `Host` header is a name other than `localhost`.
Never forward the port to a public address.

## How the bot uses it

| Tool | Does | Approval |
| --- | --- | --- |
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
point somewhere else, which is one more reason actions are approved by
default. Each chat remembers its current tab.

When a page needs a person (a sign-in, a two-factor code, a captcha, a
consent screen) the bot's context tells it to stop and ask the user to do
it in the `takeover` place, then snapshot again. It never asks for
passwords in chat.

All options are in the [bot reference](bots.md#browser).
