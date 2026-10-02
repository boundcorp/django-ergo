"""Webhooks for bot plugins.

A plugin lists its handlers in ``webhooks()``; the project mounts one URL
pattern for all of them and says which bots are live::

    # urls.py
    path("hooks/", include("django_ergo.bots.urls")),

    # at startup
    from django_ergo.bots import webhooks
    webhooks.set_registry(BotRegistry.discover("bots/"))
    # or a function, called on the first webhook request:
    webhooks.set_registry(load_my_bots)

A request to ``hooks/<bot>/<plugin>/<name>/`` calls that plugin's handler
with the request. Unknown bots, plugins or names get a 404. Handlers check
their own secrets (Telegram's secret-token header, a signature).
"""

from __future__ import annotations

import inspect
import logging
from typing import TYPE_CHECKING

from django.http import Http404
from django.http import HttpResponse
from django.http import JsonResponse

if TYPE_CHECKING:
    from collections.abc import Callable

    from django_ergo.bots.registry import BotRegistry

logger = logging.getLogger(__name__)

_registry: BotRegistry | Callable[[], BotRegistry] | None = None


def set_registry(registry: BotRegistry | Callable[[], BotRegistry] | None) -> None:
    """The bots whose webhooks this process serves, or a function loading them."""
    global _registry  # noqa: PLW0603 — one registry per process
    _registry = registry


def get_registry() -> BotRegistry | None:
    global _registry  # noqa: PLW0603
    if callable(_registry) and not hasattr(_registry, "bots"):
        _registry = _registry()
    return _registry


def find_handler(bot_name: str, plugin_name: str, hook: str):
    registry = get_registry()
    if registry is None or bot_name not in registry:
        return None
    plugin = registry.get(bot_name).plugin(plugin_name)
    if plugin is None:
        return None
    return plugin.webhooks().get(hook)


async def webhook_view(request, bot: str, plugin: str, hook: str):
    handler = find_handler(bot, plugin, hook)
    if handler is None:
        raise Http404
    result = handler(request)
    if inspect.isawaitable(result):
        result = await result
    if isinstance(result, HttpResponse):
        return result
    if result is None:
        return HttpResponse(status=200)
    return JsonResponse(result, safe=False)



# Set directly: Django < 5's @csrf_exempt wraps an async view in a sync one.
webhook_view.csrf_exempt = True
