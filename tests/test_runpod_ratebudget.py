"""The durable per-key RunPod REST budget shared by controller and maintenance."""

import pytest

from django_ergo.plugins.runpod.ratebudget import LIMITS
from django_ergo.plugins.runpod.ratebudget import RESERVE
from django_ergo.plugins.runpod.ratebudget import RestBudget
from django_ergo.plugins.runpod.transport import RateLimitError


@pytest.fixture
def roles(tmp_path):
    """Two processes (bot-tasks and maintenance) on one key's budget file."""
    clock = [1_000_000.0]
    path = tmp_path / "root" / ".rest-budget-KEY.sqlite3"
    controller = RestBudget(path, clock=lambda: clock[0])
    maintenance = RestBudget(path, clock=lambda: clock[0])
    return clock, controller, maintenance


def test_roles_share_one_minute_budget_and_keep_a_cleanup_reserve(roles):
    clock, controller, maintenance = roles
    limit = LIMITS["minute"][1]
    ordinary = limit - RESERVE["minute"]
    for index in range(ordinary):
        (controller if index % 2 else maintenance).acquire()
    with pytest.raises(RateLimitError) as refused:
        controller.acquire()
    assert 0 < refused.value.retry_after <= 60
    with pytest.raises(RateLimitError):
        maintenance.acquire()
    # Teardown can still spend the reserve, up to the provider's own limit.
    for _ in range(RESERVE["minute"]):
        maintenance.acquire(priority=True)
    with pytest.raises(RateLimitError):
        maintenance.acquire(priority=True)
    clock[0] += 61
    controller.acquire()


def test_provider_headers_count_calls_made_elsewhere_on_the_key(roles):
    clock, controller, maintenance = roles
    floor = RESERVE["hour"]
    controller.observe(
        {
            "ratelimit": f'"minute";r=11;t=60, "hour";r={floor + 1};t=900, "day";r=900;t=9000'
        },
        200,
    )
    maintenance.acquire()
    with pytest.raises(RateLimitError) as refused:
        controller.acquire()
    assert refused.value.retry_after == 900
    controller.acquire(priority=True)
    # The provider's window reset restores the ordinary budget.
    clock[0] += 901
    controller.acquire()


def test_429_blocks_every_role_until_retry_after_plus_jitter(roles):
    clock, controller, maintenance = roles
    controller.observe({"Retry-After": "29"}, 429)
    for role, priority in ((controller, False), (maintenance, True)):
        with pytest.raises(RateLimitError) as refused:
            role.acquire(priority=priority)
        assert 29 <= refused.value.retry_after <= 35
    clock[0] += 35
    maintenance.acquire()


def test_daily_reserve_keeps_cleanup_possible(roles):
    _clock, controller, maintenance = roles
    controller.observe(
        {"ratelimit": f'"day";r={RESERVE["day"]};t=4000'},
        200,
    )
    with pytest.raises(RateLimitError):
        controller.acquire()
    maintenance.acquire(priority=True)
