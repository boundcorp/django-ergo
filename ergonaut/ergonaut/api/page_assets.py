"""Signed URLs for the assets of a sandboxed bot-folder page.

A bot-folder ``.jhtml`` page is served with ``Content-Security-Policy: sandbox`` (see
``ergonaut.api.bots.page_response``), so the browser gives it an opaque origin and sends
no cookies with the requests it makes: its scripts, styles and images would be refused
as not logged in, and its module scripts need CORS. ``render_page`` therefore points the
page's relative asset references at ``/api/bots/<bot>/assets/<token>/<path>`` (``asset_urls``),
which the ``bot_asset`` route serves for the user the token was made for, for that bot's
folder only, for an hour, with ``Access-Control-Allow-Origin: *``.

The token is a path segment, not a ``?sig=`` query parameter, so that what an asset
refers to in turn (a module's ``import "./util.mjs"``, a style sheet's ``url(logo.svg)``,
``new URL("x.png", import.meta.url)``) resolves under the same token: relative URLs keep
the path but drop the query. A token never opens a page: ``.jhtml`` files render with
data and need a login.
"""

from __future__ import annotations

from collections.abc import Callable
from urllib.parse import quote

from django.contrib.auth import get_user_model
from django.core import signing

SALT = "ergonaut.page_asset"
MAX_AGE_SECONDS = 60 * 60


def make_token(user, bot_name: str) -> str:
    # signing puts ":" between its parts; "~" is a plain path character.
    return signing.dumps({"u": str(user.pk), "b": bot_name}, salt=SALT).replace(":", "~")


def user_for_token(token: str, bot_name: str):
    """The user a token was made for on ``bot_name``'s files, or None (bad, expired, or for another bot)."""
    try:
        data = signing.loads(token.replace("~", ":"), salt=SALT, max_age=MAX_AGE_SECONDS)
    except signing.BadSignature:
        return None
    if data.get("b") != bot_name:
        return None
    return get_user_model().objects.filter(pk=data.get("u"), is_active=True).first()


def asset_urls(user, bot_name: str) -> Callable[[str], str]:
    """Maps a bot-relative path to the URL the sandboxed page loads it from."""
    base = f"/api/bots/{quote(bot_name, safe='')}/assets/{make_token(user, bot_name)}/"

    def url(path: str) -> str:
        return base + quote(path)

    return url
