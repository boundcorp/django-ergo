"""The Google authorization-code flow with PKCE, with nothing sensitive logged or returned.

Every function here returns fixed reason codes (``REASONS``), never text from Google. Codes,
tokens, client secrets and Google's responses stay in local variables."""

from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets
import time
from urllib.parse import urlencode

import requests
from django.conf import settings

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"  # noqa: S105 - public endpoint
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
ISSUERS = {"https://accounts.google.com", "accounts.google.com"}
STATE_TTL = 600  # seconds a started sign-in stays valid
MAX_PENDING = 5
SESSION_KEY = "google_oauth_flows"
FLASH_KEY = "google_oauth_flash"
HTTP_TIMEOUT = 15
MAX_BODY = 100_000

REASONS = {
    "connected": "Connected.",
    "denied": "Google sign-in was cancelled or refused.",
    "state": "That sign-in link expired or was already used. Start again.",
    "not_configured": "The Google client isn't configured on the server.",
    "exchange": "Google didn't accept the sign-in. Start again.",
    "no_refresh": "Google gave no long-lived access. Remove this app at myaccount.google.com/permissions and try again.",
    "scope": "Not every requested permission was granted. Connect again and leave every box ticked.",
    "identity": "Couldn't verify which Google account signed in.",
    "account": "A different account signed in than the one asked for.",
    "store": "The account was verified but couldn't be saved.",
    "disconnected": "Disconnected and revoked at Google.",
    "disconnected_local": "Disconnected here. Google didn't confirm the revoke: remove the app at myaccount.google.com/permissions.",
    "unknown_account": "No such connected account.",
}
EMAIL = re.compile(r"^[^@\s<>\"',;]{1,64}@[A-Za-z0-9.-]{1,190}$")
DOMAIN = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+$")


def redirect_uri() -> str:
    """Ergonaut's own HTTPS callback, or "" when no public address is set."""
    base = (getattr(settings, "ERGONAUT_PUBLIC_URL", "") or getattr(settings, "BASE_URL", "") or "").rstrip("/")
    local = base.startswith(("http://localhost", "http://127.0.0.1"))
    if not (base.startswith("https://") or local):
        return ""
    return base + "/oauth/google/callback/"


def check_login_hint(value: str) -> str | None:
    value = (value or "").strip()
    if not value:
        return ""
    return value if EMAIL.match(value) else None


def check_hd(value: str) -> str | None:
    """ "" (any account), "consumer" (a Google account with no Workspace domain), or a domain."""
    value = (value or "").strip().lower()
    if value in ("", "consumer"):
        return value
    return value if DOMAIN.match(value) else None


def _pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode()).digest()
    return verifier, base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def begin(request, connector, client_id: str, *, login_hint: str, hd: str, now: float | None = None) -> str:
    """Remember a one-use pending sign-in in the user's session and return Google's URL."""
    now = now or time.time()
    pending = _live(request.session.get(SESSION_KEY), now)
    while len(pending) >= MAX_PENDING:
        pending.pop(min(pending, key=lambda k: pending[k]["at"]))
    state, nonce = secrets.token_urlsafe(32), secrets.token_urlsafe(24)
    verifier, challenge = _pkce()
    pending[state] = {
        "bot": connector.bot_name,
        "user": request.user.pk,
        "verifier": verifier,
        "nonce": nonce,
        "hint": login_hint,
        "hd": hd,
        "at": now,
    }
    request.session[SESSION_KEY] = pending
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri(),
        "response_type": "code",
        "scope": " ".join([*connector.scopes, "openid", "email"]),
        "state": state,
        "nonce": nonce,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "true",
    }
    if login_hint:
        params["login_hint"] = login_hint
    if hd and hd != "consumer":
        params["hd"] = hd
    return AUTH_URL + "?" + urlencode(params)


def _live(pending, now: float) -> dict:
    if not isinstance(pending, dict):
        return {}
    return {k: v for k, v in pending.items() if isinstance(v, dict) and now - v.get("at", 0) < STATE_TTL}


def take(request, state: str, now: float | None = None) -> dict | None:
    """The pending sign-in for ``state``, removed so it can't be used twice. None unless it is
    live, belongs to this session and this user."""
    now = now or time.time()
    pending = _live(request.session.get(SESSION_KEY), now)
    entry = pending.pop(state, None) if isinstance(state, str) else None
    request.session[SESSION_KEY] = pending
    if entry is None or entry.get("user") != request.user.pk:
        return None
    return entry


def post(url: str, data: dict) -> tuple[int, dict]:
    """POST a form to Google. Returns (status, json) only; transport detail is dropped."""
    try:
        response = requests.post(url, data=data, timeout=HTTP_TIMEOUT, allow_redirects=False)
        body = json.loads(response.content[:MAX_BODY] or b"{}")
        return response.status_code, body if isinstance(body, dict) else {}
    except Exception:  # noqa: BLE001 - never leak transport detail
        return 0, {}


def id_token_claims(id_token) -> dict | None:
    """The claims of an id_token that came straight from Google's token endpoint over TLS (so,
    per OpenID Connect Core 3.1.3.7, its signature needn't be checked again)."""
    try:
        payload = id_token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except Exception:  # noqa: BLE001
        return None
    return claims if isinstance(claims, dict) else None


def exchange(entry: dict, connector, client_id: str, client_secret: str, code: str, now: float | None = None):
    """Trade the code for tokens and verify them. Returns (account dict, None) or (None, reason)."""
    now = now or time.time()
    status, body = post(
        TOKEN_URL,
        {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri(),
            "client_id": client_id,
            "client_secret": client_secret,
            "code_verifier": entry["verifier"],
        },
    )
    if status != 200 or not isinstance(body.get("access_token"), str):
        return None, "exchange"
    refresh = body.get("refresh_token")
    if not isinstance(refresh, str) or not refresh:
        _revoke_quietly(body.get("access_token"))
        return None, "no_refresh"
    granted = set(str(body.get("scope") or "").split())
    if not set(connector.scopes) <= granted:
        _revoke_quietly(refresh)
        return None, "scope"
    claims = id_token_claims(body.get("id_token") or "")
    email = claims.get("email") if claims else None
    ok = (
        claims is not None
        and claims.get("iss") in ISSUERS
        and claims.get("aud") == client_id
        and isinstance(claims.get("exp"), int | float)
        and claims["exp"] > now
        and claims.get("nonce") == entry["nonce"]
        and isinstance(claims.get("sub"), str)
        and isinstance(email, str)
        and EMAIL.match(email)
        and claims.get("email_verified") in (True, "true")
    )
    if not ok:
        _revoke_quietly(refresh)
        return None, "identity"
    email = email.lower()
    domain = claims.get("hd") if isinstance(claims.get("hd"), str) else ""
    wanted = entry.get("hd") or ""
    if (
        (entry.get("hint") and email != entry["hint"].lower())
        or (wanted == "consumer" and domain)
        or (wanted not in ("", "consumer") and domain.lower() != wanted)
    ):
        _revoke_quietly(refresh)
        return None, "account"
    return {
        "email": email,
        "label": email,
        "scopes": sorted(granted),
        "refresh_token": refresh,
        "client_id": client_id,
        "client_secret": client_secret,
        "google_sub": claims["sub"],
        "hd": domain,
    }, None


def _revoke_quietly(token) -> None:
    if isinstance(token, str) and token:
        post(REVOKE_URL, {"token": token})


def revoke(token: str) -> bool:
    status, _ = post(REVOKE_URL, {"token": token})
    return status == 200
