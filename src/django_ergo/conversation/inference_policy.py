"""Private inference policy: who may send to a private pool, and the checks
every network send makes.

A private pool (``private_pools:`` in providers.yaml, parsed by
``bots.providers``) is a set of API-billed models only the bots it lists in
``allowed_bots`` may use. Bots select them as ``personal`` (the pool's
default member) or ``personal/<alias>``. Nothing else (auto tiers, routers,
retries, defaults, saved session state, other bots) can reach them:

- Trusted resolution code (``Bot.make_engine``) mints a :class:`PrivateGrant`
  from the bot's reviewed config with :func:`mint_grant` and attaches it to
  the private engine it just built. Grants live in memory only: they are
  never read from session metadata, engine config dicts, prompts or
  messages, can't be copied or pickled, and belong to one engine (one turn).
- The private engine (``engines.fireworks_api``) calls :func:`check_grant`
  before every HTTP attempt, before it reads its API key, and raises
  :class:`PolicyError` (never retried, never turned into a fallback) when
  there is no valid grant for the exact model it is about to send.
- Public engines refuse private endpoints (:func:`assert_public_endpoint`),
  so an OpenAI-compatible client with a custom ``base_url`` can't reach them.
- Subprocesses get environments without protected variables
  (:func:`scrubbed_env`), and ``ToolContext.secret`` won't read them.

These are in-process guards: they stop Ergo's own code paths from routing a
non-allowlisted bot to a private model. They are not a sandbox. Code that
runs in the same process or as the same OS user (a bash tool, a Python tool)
could still read the key from the process environment or ``/proc``. Running
private inference in a separate, credential-isolated service is the planned
second phase (see docs/bots.md, "Private inference").
"""

from __future__ import annotations

import os
import time
import weakref
from dataclasses import dataclass
from dataclasses import field
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

if TYPE_CHECKING:
    from collections.abc import Mapping
    from decimal import Decimal

FIREWORKS = "fireworks"
FIREWORKS_BASE_URL = "https://api.fireworks.ai/inference/v1"
FIREWORKS_HOST = "api.fireworks.ai"
FIREWORKS_KEY_ENV = "FIREWORKS_API_KEY"
# Providers a private pool can use, and the domains only private engines may call.
PRIVATE_PROVIDERS = (FIREWORKS,)
PRIVATE_DOMAINS = ("fireworks.ai",)
# Engine types only a private pool selector can build (never a bot or settings default).
PRIVATE_ENGINE_TYPES = (FIREWORKS,)
# Engine config that would change where a private engine sends or how it
# authenticates; neither providers.yaml nor bot.yaml may set it.
PRIVATE_RESERVED_CONFIG = frozenset(
    {
        "api_key",
        "api_key_env",
        "base_url",
        "api_base",
        "endpoint",
        "url",
        "organization",
        "project",
        "default_headers",
        "default_query",
        "http_client",
        "max_retries",
        "grant",
    }
)
# Model IDs in this namespace are Fireworks-hosted: private only.
PRIVATE_MODEL_MARKERS = ("accounts/fireworks/",)
# Selector namespaces that are always private, configured or not, so a bot that
# asks for ``personal`` without a configured pool fails instead of falling back.
RESERVED_POOL_NAMES = ("personal",)
# Environment variables subprocesses and tools never see (extend with
# DJANGO_ERGO["PROTECTED_ENV_PREFIXES"]).
DEFAULT_PROTECTED_ENV_PREFIXES = ("FIREWORKS_",)
# A grant covers one engine, built for one turn; this bounds a stray reference.
GRANT_TTL_SECONDS = 6 * 3600


class PolicyError(RuntimeError):
    """A private-inference policy refusal. Never retried, never a fallback."""


class SpendCapError(PolicyError):
    """A private pool's spend cap would be passed (or the cost can't be bounded)."""


@dataclass(frozen=True, eq=False)
class PrivatePool:
    """One ``private_pools`` entry from providers.yaml (host-controlled)."""

    name: str
    provider: str
    enabled: bool = False
    allowed_bots: frozenset[str] = frozenset()
    models: Mapping[str, str] = field(default_factory=dict)  # alias -> exact model ID
    default: str = ""
    daily_usd_cap: Decimal | None = None

    def resolve(self, alias: str) -> tuple[str, str]:
        """(alias, exact model ID) for ``alias`` ("" = the pool's default)."""
        alias = alias or self.default
        if not alias:
            msg = f"Private pool {self.name!r} has no default; pick {self.name}/<alias>"
            raise PolicyError(msg)
        model_id = self.models.get(alias)
        if model_id is None:
            known = ", ".join(f"{self.name}/{a}" for a in self.models) or "none"
            msg = (
                f"Private pool {self.name!r} has no member {alias!r} (members: {known})"
            )
            raise PolicyError(msg)
        return alias, model_id

    def check_bot(self, bot: str) -> None:
        if not bot or bot not in self.allowed_bots:
            msg = (
                f"Bot {bot!r} isn't allowed to use private pool {self.name!r} "
                f"(allowed_bots in providers.yaml)"
            )
            raise PolicyError(msg)
        if not self.enabled:
            msg = f"Private pool {self.name!r} is disabled (enabled: false in providers.yaml)"
            raise PolicyError(msg)

    @property
    def model_ids(self) -> frozenset[str]:
        return frozenset(self.models.values())


_MINT = object()
_ISSUED: weakref.WeakSet[PrivateGrant] = weakref.WeakSet()


class PrivateGrant:
    """Permission for one engine to send to one exact private model.

    Only :func:`mint_grant` makes one; a grant built any other way, copied or
    unpickled isn't in the issued set and fails :func:`check_grant`.
    """

    __slots__ = (
        "__weakref__",
        "alias",
        "bot",
        "expires_at",
        "model_id",
        "pool",
        "purpose",
        "selector",
        "session_id",
        "user_id",
    )

    def __init__(  # noqa: PLR0913
        self,
        *,
        _mint,
        pool: PrivatePool,
        bot: str,
        alias: str,
        model_id: str,
        selector: str,
        user_id=None,
        session_id=None,
        purpose: str = "chat",
    ):
        if _mint is not _MINT:
            msg = "Private grants are minted by inference_policy.mint_grant only"
            raise PolicyError(msg)
        fields = {
            "pool": pool,
            "bot": bot,
            "alias": alias,
            "model_id": model_id,
            "selector": selector,
            "user_id": user_id,
            "session_id": session_id,
            "purpose": purpose,
            "expires_at": time.monotonic() + GRANT_TTL_SECONDS,
        }
        for name, value in fields.items():
            object.__setattr__(self, name, value)

    def __setattr__(self, name, value):
        msg = "Private grants can't be changed"
        raise TypeError(msg)

    def __delattr__(self, name):
        msg = "Private grants can't be changed"
        raise TypeError(msg)

    def __repr__(self) -> str:
        return f"<PrivateGrant {self.bot} -> {self.pool.name}/{self.alias} ({self.purpose})>"

    def __reduce__(self):
        msg = "Private grants can't be serialized"
        raise TypeError(msg)

    def __copy__(self):
        msg = "Private grants can't be copied"
        raise TypeError(msg)

    def __deepcopy__(self, memo):
        msg = "Private grants can't be copied"
        raise TypeError(msg)


def mint_grant(  # noqa: PLR0913
    pool: PrivatePool,
    *,
    bot: str,
    selector: str,
    user_id=None,
    session_id=None,
    purpose: str = "chat",
) -> PrivateGrant:
    """A grant for ``bot`` to send to ``selector``'s model in ``pool``.

    Call it only from code that resolved ``selector`` from the bot's own
    reviewed config (or an authenticated selection on that bot's chat).
    """
    head, _, alias = selector.partition("/")
    if head != pool.name:
        msg = f"Selector {selector!r} isn't in private pool {pool.name!r}"
        raise PolicyError(msg)
    alias, model_id = pool.resolve(alias)
    pool.check_bot(bot)
    grant = PrivateGrant(
        _mint=_MINT,
        pool=pool,
        bot=bot,
        alias=alias,
        model_id=model_id,
        selector=selector,
        user_id=user_id,
        session_id=session_id,
        purpose=purpose,
    )
    _ISSUED.add(grant)
    return grant


def check_grant(grant, *, model_id: str) -> PrivateGrant:
    """``grant`` if it allows sending to ``model_id`` now, else PolicyError."""
    if not isinstance(grant, PrivateGrant) or grant not in _ISSUED:
        msg = "No valid private-inference grant for this request; refusing to send"
        raise PolicyError(msg)
    if time.monotonic() >= grant.expires_at:
        msg = "The private-inference grant has expired; refusing to send"
        raise PolicyError(msg)
    pool = grant.pool
    pool.check_bot(grant.bot)
    if model_id != grant.model_id or model_id not in pool.model_ids:
        msg = (
            f"The private-inference grant is for {grant.model_id!r}, "
            f"not {model_id!r}; refusing to send"
        )
        raise PolicyError(msg)
    return grant


# -- endpoints ----------------------------------------------------------------


def is_private_host(host: str | None) -> bool:
    host = (host or "").strip().lower().rstrip(".")
    return any(host == d or host.endswith(f".{d}") for d in PRIVATE_DOMAINS)


def url_host(url) -> str:
    text = str(url or "").strip()
    if not text:
        return ""
    if "://" not in text:
        text = f"//{text}"
    try:
        return urlsplit(text).hostname or ""
    except ValueError:
        return text


def assert_public_endpoint(url, *, what: str = "This engine") -> None:
    """PolicyError when ``url`` (a base URL or request URL) is a private
    inference endpoint, which only the guarded private engine may call."""
    if is_private_host(url_host(url)):
        msg = (
            f"{what} refuses the private inference endpoint {url_host(url)}: "
            "only an allowlisted bot's private pool may call it"
        )
        raise PolicyError(msg)


def effective_openai_base_url(base_url=None) -> str:
    """The base URL an OpenAI SDK client built with ``base_url`` would use."""
    return str(
        base_url or os.environ.get("OPENAI_BASE_URL") or "https://api.openai.com/v1"
    )


def effective_anthropic_base_url(base_url=None) -> str:
    """The base URL an Anthropic SDK client built with ``base_url`` would use."""
    return str(
        base_url or os.environ.get("ANTHROPIC_BASE_URL") or "https://api.anthropic.com"
    )


def names_private_model(text) -> bool:
    """A model string in a private namespace: a Fireworks model ID or provider
    (``accounts/fireworks/...``, ``fireworks/...``) or a reserved pool name."""
    text = str(text or "").strip().lower()
    if not text:
        return False
    if any(marker in text for marker in PRIVATE_MODEL_MARKERS):
        return True
    if any(provider in text for provider in PRIVATE_PROVIDERS):
        return True
    return text.partition("/")[0] in RESERVED_POOL_NAMES


# -- environment --------------------------------------------------------------


def protected_env_prefixes() -> tuple[str, ...]:
    """Name prefixes of environment variables tools and subprocesses never get.
    Settings can add to the defaults, never remove them."""
    extra: list = []
    try:
        from django_ergo.settings import api_settings

        extra = list(getattr(api_settings, "PROTECTED_ENV_PREFIXES", None) or [])
    except Exception:  # noqa: BLE001 - settings not configured: defaults still apply
        extra = []
    return tuple(
        dict.fromkeys(
            [*DEFAULT_PROTECTED_ENV_PREFIXES, *(str(p).upper() for p in extra if p)]
        )
    )


def is_protected_env(name) -> bool:
    return str(name or "").upper().startswith(protected_env_prefixes())


def scrubbed_env(env=None) -> dict[str, str]:
    """A copy of ``env`` (default: this process's) without protected variables."""
    source = os.environ if env is None else env
    prefixes = protected_env_prefixes()
    return {k: v for k, v in source.items() if not str(k).upper().startswith(prefixes)}
