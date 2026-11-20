"""Reading a login CLI's terminal output, and checking what Lee pastes back.

Everything here works on values held in memory. Nothing is logged, and errors name the
problem in our own words, never the value."""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode, urlsplit

# CSI, OSC (including OSC 8 hyperlinks, ended by BEL or ST), other two-byte escapes.
_ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-Z\\-_]")
_URL = re.compile(r"https://[^\s\"'<>`\x00-\x1f]+")
# Device user codes as the CLIs print them: ABCD-EFGH, ABCD-12345, or 8 letters/digits.
# omp: "Enter code: ABCD-12345"; codex: the code alone on the line after "one-time code".
_CODE_LABELLED = re.compile(
    r"(?im)^\s*(?:enter\s+(?:the\s+)?code|user\s+code)\s*:\s*([A-Za-z0-9]{3,10}(?:-[A-Za-z0-9]{3,10}){0,2})\s*$"
)
_CODE_BARE = re.compile(r"(?m)^\s*([A-Z0-9]{3,8}(?:-[A-Z0-9]{3,8}){1,2})\s*$")
_EXPIRES = re.compile(r"(?i)expires?\s+in\s+(\d{1,4})\s*(second|minute|min|sec|s|m)")
_PASTE_PROMPT = re.compile(r"(?i)paste\s+(?:the\s+)?(?:authori[sz]ation\s+)?code")
_PASTE_CODE = re.compile(r"[A-Za-z0-9_\-#.~]{10,600}")


def clean(raw: bytes | str) -> str:
    text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw
    text = _ANSI.sub("", text)
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _host_allowed(host: str, hosts: frozenset[str]) -> bool:
    host = host.lower().rstrip(".")
    return any(host == allowed or host.endswith("." + allowed) for allowed in hosts)


def find_url(text: str, hosts: frozenset[str]) -> str | None:
    """The first https link to an allowed sign-in host, or None."""
    for match in _URL.finditer(text):
        url = match.group(0).rstrip(").,;]")
        try:
            parts = urlsplit(url)
        except ValueError:
            continue
        if parts.scheme == "https" and parts.hostname and not parts.username and _host_allowed(parts.hostname, hosts):
            return url
    return None


def find_code(text: str) -> str | None:
    match = _CODE_LABELLED.search(text) or _CODE_BARE.search(text)
    return match.group(1) if match else None


def url_state(url: str) -> str | None:
    values = [v for k, v in parse_qsl(urlsplit(url).query, keep_blank_values=True) if k == "state"]
    return values[0] if len(values) == 1 and values[0] else None


def expires_in(text: str) -> int | None:
    match = _EXPIRES.search(text)
    if not match:
        return None
    number, unit = int(match.group(1)), match.group(2).lower()
    return number * 60 if unit.startswith("m") else number


def paste_prompt_seen(text: str) -> bool:
    return bool(_PASTE_PROMPT.search(text))


@dataclass(frozen=True)
class Shown:
    """What the page shows: the sign-in link and, for device flows, the code."""

    url: str
    code: str | None
    state: str | None
    seconds: int | None


def read_output(text: str, shape: str, hosts: frozenset[str]) -> Shown | None:
    """What to show, once the output has all of it; None until then."""
    url = find_url(text, hosts)
    if url is None:
        return None
    if shape == "device_code":
        code = find_code(text[text.find("https://") :] if "https://" in text else text) or find_code(text)
        if code is None:
            return None
        return Shown(url=url, code=code, state=None, seconds=expires_in(text))
    if shape == "paste_redirect":
        state = url_state(url)
        if state is None:
            return None
        return Shown(url=url, code=None, state=state, seconds=None)
    return Shown(url=url, code=None, state=url_state(url), seconds=None)


def redirect_uri(url: str) -> str | None:
    values = [v for k, v in parse_qsl(urlsplit(url).query) if k == "redirect_uri"]
    return values[0] if len(values) == 1 else None


class InputError(ValueError):
    """The pasted value was refused; the message never repeats it."""


def check_paste_code(value: str, *, state: str | None = None) -> str:
    """One line of code characters. When the sign-in link carried a state, the code must be
    ``<code>#<state>`` with that state, so a code from another attempt is refused here."""
    import hmac

    value = (value or "").strip()
    if "\n" in value or "\r" in value:
        raise InputError("Paste one line: the code the sign-in page showed.")
    if not _PASTE_CODE.fullmatch(value):
        raise InputError("That doesn't look like a sign-in code.")
    if state:
        _, sep, given = value.partition("#")
        if not sep:
            raise InputError("Copy all of the code, including the # in the middle.")
        if not hmac.compare_digest(given.encode(), state.encode()):
            raise InputError("That code is from another sign-in attempt.")
    return value


def check_redirect(value: str, *, port: int, state: str | None) -> str:
    """The callback URL to request on the login host, rebuilt from the checked parts.

    It must be http, host localhost or 127.0.0.1, the store's port, path /callback, carry a
    code, and its state must equal the one in the sign-in link."""
    import hmac

    value = (value or "").strip()
    if not value or len(value) > 4000 or any(ch.isspace() for ch in value):
        raise InputError("Paste the whole address from the browser's address bar.")
    try:
        parts = urlsplit(value)
        given_port = parts.port
    except ValueError as exc:
        raise InputError("That isn't a URL.") from exc
    if parts.scheme != "http" or parts.hostname not in {"localhost", "127.0.0.1"}:
        raise InputError("The address must start with http://localhost.")
    if parts.username or parts.password:
        raise InputError("That isn't the sign-in callback address.")
    if given_port != port:
        raise InputError(f"The address must be on port {port}.")
    if parts.path != "/callback":
        raise InputError("The address must end in /callback?…")
    params = parse_qsl(parts.query, keep_blank_values=True, strict_parsing=False)
    names = [name for name, _ in params]
    if names.count("code") != 1 or not dict(params).get("code"):
        raise InputError("The address has no sign-in code.")
    if names.count("state") != 1 or not state:
        raise InputError("The address doesn't match this sign-in.")
    if not hmac.compare_digest(dict(params)["state"].encode(), state.encode()):
        raise InputError("The address doesn't match this sign-in (it's from another attempt).")
    return f"http://127.0.0.1:{port}/callback?{urlencode(params)}"
