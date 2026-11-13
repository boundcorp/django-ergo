from dataclasses import replace
from decimal import Decimal
from decimal import localcontext

import pytest

from django_ergo.plugins.runpod.budget import Evidence
from django_ergo.plugins.runpod.budget import RentalTimeline
from django_ergo.plugins.runpod.budget import admit_rental

D = Decimal
TIMELINE = RentalTimeline(3600, 100, 200, 300, 2400, 300, 300)


def request(**changes):
    arguments = {
        "timeline": TIMELINE,
        "compute_hourly_usd": Evidence(D("2.50"), True),
        "storage_usd": Evidence(D("0.20"), True),
        "network_usd": Evidence(D("0.20"), True),
        "fees_usd": Evidence(D("0.10"), True),
        "project_cap_usd": D("5.00"),
        "project_committed_usd": D("2.00"),
        "rental_cap_usd": D("3.00"),
    }
    arguments.update(changes)
    return admit_rental(**arguments)


def test_exact_global_and_project_boundaries_are_admitted():
    result = request()
    assert result.allowed
    assert result.reservation_usd == D("3.00")


def test_all_rental_time_is_charged_not_just_phases():
    result = request(timeline=RentalTimeline(1800, 0, 0, 0, 0, 0, 0))
    assert result.allowed
    assert result.reservation_usd == D("1.75")


def test_round_up_exact_fraction_without_decimal_context_rounding():
    with localcontext() as context:
        context.prec = 2
        result = request(
            timeline=RentalTimeline(1, 0, 0, 0, 0, 0, 1),
            compute_hourly_usd=Evidence(D("1"), True),
            storage_usd=Evidence(D("0"), True),
            network_usd=Evidence(D("0"), True),
            fees_usd=Evidence(D("0"), True),
        )
    assert result.reservation_usd == D("0.01")


def test_global_cap_includes_ancillaries_and_rounding():
    result = request(fees_usd=Evidence(D("0.10001"), True))
    assert not result.allowed
    assert result.reason == "hard authorization exceeded"
    assert result.reservation_usd == D("3.01")


def test_per_rental_cap_is_policy_data():
    assert request(project_cap_usd=D("10"), rental_cap_usd=D("3.01")).allowed
    lowered = request(rental_cap_usd=D("2.99"))
    assert not lowered.allowed
    assert lowered.reason == "hard authorization exceeded"


def test_project_cap_includes_existing_commitments():
    result = request(project_committed_usd=D("2.01"))
    assert not result.allowed
    assert result.reason == "project cap exceeded"
    assert result.reservation_usd == D("3.00")


@pytest.mark.parametrize(
    "field", ["compute_hourly_usd", "storage_usd", "network_usd", "fees_usd"]
)
@pytest.mark.parametrize(
    "evidence",
    [
        None,
        Evidence(None, True),
        Evidence(D("0"), False),
        Evidence(D("-0.01"), True),
        Evidence(D("NaN"), True),
        Evidence(D("Infinity"), True),
        Evidence(0, True),
    ],
)
def test_unknown_unverified_and_invalid_pricing_fails_closed(field, evidence):
    result = request(**{field: evidence})
    assert not result.allowed
    assert result.reservation_usd is None


@pytest.mark.parametrize(
    "field", ["project_cap_usd", "project_committed_usd", "rental_cap_usd"]
)
@pytest.mark.parametrize("amount", [None, D("-1"), D("NaN"), D("Infinity"), 1])
def test_invalid_project_amounts_fail_closed(field, amount):
    assert not request(**{field: amount}).allowed


@pytest.mark.parametrize(
    "timeline",
    [
        replace(TIMELINE, rental_seconds=3601),
        replace(TIMELINE, rental_seconds=0),
        replace(TIMELINE, termination_seconds=301),
        replace(TIMELINE, startup_seconds=-1),
        replace(TIMELINE, rental_seconds=True),
        None,
    ],
)
def test_invalid_timeline_fails_closed(timeline):
    assert not request(timeline=timeline).allowed


def test_verified_zero_ancillaries_are_allowed():
    result = request(
        storage_usd=Evidence(D("0"), True),
        network_usd=Evidence(D("0"), True),
        fees_usd=Evidence(D("0"), True),
    )
    assert result.allowed
    assert result.reservation_usd == D("2.50")
