"""Revisions for deploy watches (``django_ergo.bots.live_watch``).

``resolve`` turns what a bot asked for into the full django-ergo commit:
a full SHA as it is, a short SHA through GitHub, and an image digest through
the registry, from the image's own config (the ``ERGONAUT_VERSION`` the build
baked in, or its ``org.opencontainers.image.revision`` label). A digest is
content-addressed, so the commit read from it is the one that image was built
from; it says nothing about whether the image runs anywhere.

``contains`` asks GitHub's compare API whether a running commit is the target
or a descendant of it. ``None`` means it couldn't tell (no network, unknown
commit), and the running commit doesn't count until it can.
"""

from __future__ import annotations

import base64
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request

from ergonaut.upgrades import github, settings

FULL_SHA = re.compile(r"^[0-9a-f]{40}$")
SHORT_SHA = re.compile(r"^[0-9a-f]{7,39}$")
DIGEST = re.compile(r"(?:^|@)(sha256:[0-9a-f]{64})$")
DEFAULT_IMAGE = "ghcr.io/boundcorp/ergonaut"
MANIFEST_TYPES = ", ".join(
    [
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    ]
)


def repo() -> str:
    return settings()["repo"]


def resolve(text: str) -> str:
    """The full commit SHA for a watch; ValueError (shown to the bot) if none."""
    value = text.strip().lower()
    if FULL_SHA.match(value):
        return value
    if match := DIGEST.search(value):
        return image_revision(match.group(1))
    if SHORT_SHA.match(value):
        try:
            sha, _ = github.commit(repo(), value)
        except Exception as exc:
            msg = f"GitHub couldn't resolve {text!r} in {repo()}: {exc}. Give the full commit SHA."
            raise ValueError(msg) from None
        if not sha.lower().startswith(value):
            msg = f"{text!r} resolved to {sha}, which isn't that commit. Give the full commit SHA."
            raise ValueError(msg)
        return sha.lower()
    msg = f"{text!r} is not a commit SHA or an image digest (sha256:...)"
    raise ValueError(msg)


def contains(target: str, revision: str) -> bool | None:
    """Whether ``revision`` is ``target`` or a descendant of it, per GitHub."""
    status = github.compare(repo(), target, revision)
    if status in ("ahead", "identical"):
        return True
    if status in ("behind", "diverged"):
        return False
    return None


# -- the image registry -------------------------------------------------------


def image() -> tuple[str, str]:
    """(registry host, repository) of the Ergonaut image (``ERGONAUT_IMAGE``)."""
    ref = (os.environ.get("ERGONAUT_IMAGE") or DEFAULT_IMAGE).split("@")[0]
    host, _, path = ref.partition("/")
    path = path.rsplit(":", 1)[0] if ":" in path.rsplit("/", 1)[-1] else path
    return host, path


def _open(url: str, headers: dict[str, str]):
    request = urllib.request.Request(url, headers={"User-Agent": "ergonaut-live-watch", **headers})
    with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310 (https registry)
        return json.load(response)


def _registry_token(host: str, path: str) -> str:
    """A pull token for ``path``: anonymous, or with the GitHub token for GHCR."""
    url = f"https://{host}/token?" + urllib.parse.urlencode({"scope": f"repository:{path}:pull"})
    headers = {}
    if host == "ghcr.io" and (token := github.token()):
        headers["Authorization"] = "Basic " + base64.b64encode(f"ergonaut:{token}".encode()).decode()
    return _open(url, headers).get("token", "")


def image_revision(digest: str) -> str:
    """The django-ergo commit the image ``digest`` was built from."""
    host, path = image()
    try:
        auth = {"Authorization": f"Bearer {_registry_token(host, path)}"}
        manifest = _open(f"https://{host}/v2/{path}/manifests/{digest}", {**auth, "Accept": MANIFEST_TYPES})
        if "manifests" in manifest:  # an index: the linux/amd64 image
            entry = next(
                (
                    m
                    for m in manifest["manifests"]
                    if (m.get("platform") or {}).get("os") == "linux"
                    and (m.get("platform") or {}).get("architecture") == "amd64"
                ),
                None,
            )
            if entry is None:
                msg = "no linux/amd64 image in that index"
                raise ValueError(msg)
            manifest = _open(
                f"https://{host}/v2/{path}/manifests/{entry['digest']}", {**auth, "Accept": MANIFEST_TYPES}
            )
        config = _open(f"https://{host}/v2/{path}/blobs/{manifest['config']['digest']}", auth)
    except (OSError, urllib.error.URLError, KeyError, ValueError) as exc:
        msg = f"Couldn't read image {digest} from {host}/{path} ({exc}). Give the django-ergo commit SHA instead."
        raise ValueError(msg) from None
    inner = config.get("config") or {}
    revision = (inner.get("Labels") or {}).get("org.opencontainers.image.revision", "")
    for entry in inner.get("Env") or []:
        if entry.startswith("ERGONAUT_VERSION=") and not revision:
            revision = entry.split("=", 1)[1]
    revision = revision.strip().lower()
    if not FULL_SHA.match(revision):
        msg = f"Image {digest} doesn't record the commit it was built from. Give the commit SHA instead."
        raise ValueError(msg)
    return revision
