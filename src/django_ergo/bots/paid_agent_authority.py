"""Reconstruct current bot authority for the separately hosted paid-agent proxy."""

import json
import os

from django_ergo.bots import pools
from django_ergo.bots import webhooks
from django_ergo.conversation.models import ConversationSession
from django_ergo.conversation.paid_agent_proxy import UPSTREAM
from django_ergo.conversation.paid_agent_proxy import deny
from django_ergo.conversation.paid_agent_proxy import mapped_account


def resolve_launch(launch):
    """Production resolver using Ergonaut's current registry and pool authority."""
    webhooks.refresh_registry()
    registry = webhooks.get_registry()
    captured = launch.authority
    if registry is None or captured.get("bot") not in registry:
        deny("Current bot registry unavailable")
    bot = registry.get(captured["bot"])
    session = ConversationSession.objects.get(
        pk=captured["session"], user_id=launch.user_id
    )
    privacy = pools.privacy_for(bot, session)
    if privacy.restricted:
        deny("Current privacy policy has no approved coding route")
    decision = pools.Decision(
        consumer=pools.CODING,
        pool=captured["pool"],
        provider=launch.provider,
        model=launch.allowed_models[0],
        route=f"{launch.provider}/{launch.allowed_models[0]}",
        billing="metered",
        grant_id=launch.grant_id,
        transport_identity=captured["transport_identity"],
        privacy=privacy,
        bot_lineage=tuple(captured["bot_lineage"]),
        origin=tuple(captured["origin"]),
    )
    guard = decision.guard(bot, session, purpose="paid_agent")
    provider = pools.fresh_providers(pools.live_providers(bot)).providers[
        launch.provider
    ]
    identity = pools.transport_identity(provider, guard.model)
    transport = json.loads(identity)
    if (
        provider.type != "openai"
        or provider.transport != "api"
        or identity != captured["transport_identity"]
        or transport[4] != UPSTREAM
    ):
        deny("Only explicitly mapped standard OpenAI accounts are supported")
    return (
        guard,
        os.environ.get(provider.api_key_env or "OPENAI_API_KEY", ""),
        mapped_account(identity),
    )
