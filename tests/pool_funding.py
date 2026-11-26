"""Synthetic explicit owner consent for tests; never production data."""

from django_ergo.bots import pools


def fund(providers, owner, pool="paid", amount=None):
    config = providers.pools.get(pool)
    return pools.approve_funding(
        providers,
        pool,
        by_id=owner.pk,
        budget_usd=amount if amount is not None else str(config.budget_usd),
        expires_at=config.expires_at,
        accounts=pools.funding_accounts(providers, config),
        note="Explicit synthetic test consent",
    )
