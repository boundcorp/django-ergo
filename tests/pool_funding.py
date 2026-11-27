"""Synthetic explicit owner consent for tests; never production data."""

from django_ergo.bots import pools


def current_funding(providers, pool):
    """The version and kind a client would read before writing, as the UI does."""
    from django_ergo.conversation.models import SpendFunding

    config = providers.pools.get(pool)
    from django_ergo.conversation.models import SpendEnvelope

    owner_id = pools.owner_ids(providers).get(config.owner)
    envelope = SpendEnvelope.objects.filter(
        key=pools.config_envelope_key(pool, owner_id)
    ).first()
    latest = (
        SpendFunding.objects.filter(envelope=envelope).order_by("-pk").first()
        if envelope
        else None
    )
    return (latest.pk, latest.budget_period) if latest else (0, "cumulative")


def fund(providers, owner, pool="paid", amount=None):
    config = providers.pools.get(pool)
    version, period = current_funding(providers, pool)
    return pools.approve_funding(
        providers,
        pool,
        by_id=owner.pk,
        budget_usd=amount if amount is not None else str(config.budget_usd),
        expires_at=config.expires_at,
        accounts=pools.funding_accounts(providers, config),
        note="Explicit synthetic test consent",
        budget_period=period,
        version=version,
    )
