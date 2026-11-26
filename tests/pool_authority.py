"""Construct captured test authority for low-level reservation tests."""

from types import SimpleNamespace

from django_ergo.bots import pools


def authority(source, ref="openai/gpt-6.1-sol"):
    providers = pools.live_providers(source)
    provider, _, model = ref.partition("/")
    decision = SimpleNamespace(
        route=ref,
        transport_identity=pools.transport_identity(
            providers.providers[provider], model
        ),
        privacy=pools.Privacy((), frozenset(providers.private_pools)),
        bot_lineage=(),
        origin=(),
    )
    if hasattr(source, "pools"):
        source = SimpleNamespace(name="devbox", providers=source)
    return pools.SendAuthority(source, decision=decision)
