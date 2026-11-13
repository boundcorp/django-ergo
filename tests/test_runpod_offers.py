"""Offer selection, GPU identity rules and no-capacity classification (offline)."""

import io
import json
from decimal import Decimal
from urllib.error import HTTPError

import pytest

from django_ergo.plugins.runpod.gpu_identity import gpu_mismatch
from django_ergo.plugins.runpod.hardware import catalog
from django_ergo.plugins.runpod.hardware import identity
from django_ergo.plugins.runpod.hardware import requirements
from django_ergo.plugins.runpod.offers import evaluate
from django_ergo.plugins.runpod.offers import stock_query
from django_ergo.plugins.runpod.transport import CapacityError
from django_ergo.plugins.runpod.transport import ProviderError
from django_ergo.plugins.runpod.transport import RunpodREST
from tests.runpod_stock_fixtures import NO_CAPACITY_BODY
from tests.runpod_stock_fixtures import stock

A100 = "NVIDIA A100-SXM4-80GB"
H100 = "NVIDIA H100 80GB HBM3"
PRO6000 = "NVIDIA RTX PRO 6000 Blackwell Server Edition"
L40S = "NVIDIA L40S"


def hw(**changes):
    return requirements(
        {
            "hardware": {
                "gpus": [A100, H100],
                "min_vram_gb": 48,
                "min_driver_major": 580,
                "compute_capabilities": ["8.0", "9.0"],
                **changes,
            }
        }
    )


def choose(cap="1.20", gpu_catalog=None, **changes):
    return evaluate(stock(), hw(**changes), gpu_catalog or catalog(), Decimal(cap))


def places(offers):
    return [(offer["gpu_type"], offer["data_center_id"]) for offer in offers]


def test_selects_across_every_listed_region_by_stock_level():
    offers, seen = choose()
    assert places(offers) == [(A100, "EU-RO-1"), (A100, "US-KS-2"), (A100, "US-WA-1")]
    assert offers[0]["hourly_usd"] == "1.19"
    [h100] = [row for row in seen["gpus"] if row["gpu_type"] == H100]
    assert h100["reason"] == "$2.69/h is above the $1.20/h ceiling"


def test_region_allowlist_and_exclusions_are_data():
    offers, seen = choose(data_centers=["US-WA-1", "US-KS-2"])
    assert places(offers) == [(A100, "US-KS-2"), (A100, "US-WA-1")]
    skipped = {c["id"]: c["skip"] for c in seen["gpus"][0]["data_centers"]}
    assert skipped["EU-RO-1"] == "data center not allowed"
    offers, _ = choose(exclude_data_centers=["EU-RO-1", "US-KS-2"])
    assert places(offers) == [(A100, "US-WA-1")]


def test_preference_order_beats_price_and_any_gpu_adds_cheapest_rest():
    offers, _ = choose(cap="3.00", gpus=[H100, A100])
    assert places(offers)[0] == (H100, "US-KS-2")
    assert {gpu for gpu, _ in places(offers)[1:]} == {A100}
    offers, _ = choose(
        cap="3.00",
        gpus=[A100],
        any_gpu=True,
        compute_capabilities=["8.0", "8.9", "9.0", "12.0"],
    )
    # Preferred GPU first, then every other eligible type cheapest first.
    assert [gpu for gpu, _ in places(offers)] == [
        A100,
        A100,
        A100,
        L40S,
        PRO6000,
        PRO6000,
        H100,
    ]


def test_hourly_ceiling_and_gpu_count_scale_the_price():
    offers, seen = choose(cap="1.00")
    assert offers == []
    assert seen["gpus"][0]["reason"] == "$1.19/h is above the $1.00/h ceiling"
    offers, _ = choose(cap="2.38", gpu_count=2)
    assert offers[0]["hourly_usd"] == "2.38"
    assert choose(cap="2.37", gpu_count=2)[0] == []


def test_image_compute_capability_and_vram_filter_gpus():
    offers, seen = choose(
        cap="3.00", gpus=[L40S, PRO6000, A100], compute_capabilities=["8.0"]
    )
    assert {gpu for gpu, _ in places(offers)} == {A100}
    reasons = {row["gpu_type"]: row.get("reason") for row in seen["gpus"]}
    assert reasons[L40S] == "compute 8.9 is not supported by the image"
    offers, seen = choose(cap="3.00", gpus=[L40S], min_vram_gb=80)
    assert offers == []
    assert seen["gpus"][0]["reason"] == "48 GB VRAM is below 80 GB"


def test_unknown_gpu_needs_only_a_catalog_entry_not_code():
    rtx4090 = "NVIDIA GeForce RTX 4090"
    offers, seen = choose(gpus=[rtx4090], compute_capabilities=["8.9"])
    assert offers == []
    assert seen["gpus"][0]["reason"].startswith("not in gpu_catalog")
    extended = catalog(
        {
            rtx4090: {
                "nvml_names": ["NVIDIA GeForce RTX 4090"],
                "compute_capability": "8.9",
                "vram_gb": 24,
            }
        }
    )
    offers, _ = choose(
        gpus=[rtx4090],
        compute_capabilities=["8.9"],
        min_vram_gb=24,
        gpu_catalog=extended,
    )
    assert places(offers) == [(rtx4090, "EU-RO-1")]


def test_stock_query_scopes_countries_cloud_and_cuda():
    query = stock_query(
        hw(countries=["US", "CA"], cloud_type="COMMUNITY", min_cuda_version="13.0")
    )
    assert query == {
        "include": "AVAILABILITY",
        "product": "POD",
        "cloud": "COMMUNITY",
        "count": "1",
        "countryCodes": "US,CA",
        "minCudaVersion": "13.0",
    }


def test_out_of_stock_types_are_reported_not_offered():
    _, seen = choose(cap="9", gpus=["NVIDIA H200"], compute_capabilities=["9.0"])
    assert seen["gpus"][0]["reason"] == "out of stock"


def test_requirement_typos_and_empty_preferences_are_rejected():
    with pytest.raises(ValueError, match="Unknown hardware requirement: min_vram"):
        requirements({"hardware": {"gpus": [A100], "min_vram": 48}})
    with pytest.raises(ValueError, match="gpus preference list or any_gpu"):
        requirements({"hardware": {}})


def rules(gpu, **changes):
    return identity(gpu, hw(**changes), catalog())


@pytest.mark.parametrize(
    ("gpu", "row"),
    [
        (A100, "NVIDIA A100-SXM4-80GB, 580.95.05, 81920, 8.0"),
        (A100, "A100-SXM4-80GB, 580.95.05, 81920, 8.0"),
        (H100, "NVIDIA H100 80GB HBM3, 580.82.07, 81559, 9.0"),
        (
            PRO6000,
            "NVIDIA RTX PRO 6000 Blackwell Server Edition, 580.95.05, 97887, 12.0",
        ),
        (L40S, "NVIDIA L40S, 580.95.05, 46068, 8.9"),
    ],
)
def test_verified_nvml_display_names_pass(gpu, row):
    # No compute fallback: the exact catalog display name alone must match.
    assert gpu_mismatch([row], rules(gpu, compute_capabilities=[])) is None


def test_unlisted_display_name_falls_back_to_compute_capability_and_vram():
    renamed = "NVIDIA A100 SXM4 80 GB (new driver string), 590.10, 81920, 8.0"
    assert gpu_mismatch([renamed], rules(A100)) is None
    assert "neither a verified name" in gpu_mismatch(
        [renamed], rules(A100, compute_capabilities=["9.0"])
    )


@pytest.mark.parametrize(
    ("rows", "reason"),
    [
        (["NVIDIA L40S, 580.95.05, 46068, 8.9"], "neither a verified name"),
        (["NVIDIA A100-SXM4-80GB, 570.124.06, 81920, 8.0"], "older than major 580"),
        (["NVIDIA RTX A5000, 580.95.05, 24564, 8.6"], "below 45000 MiB"),
        ([], "expected 1 GPU(s), observed 0"),
    ],
)
def test_gpu_outside_profile_is_rejected_before_download(rows, reason):
    assert reason in gpu_mismatch(rows, rules(A100))


class Refusing:
    def __init__(self, status, body):
        self.status = status
        self.body = body

    def open(self, request, timeout):
        payload = io.BytesIO(json.dumps(self.body).encode())
        raise HTTPError(request.full_url, self.status, "error", {}, payload)


@pytest.mark.parametrize(
    "body",
    [
        NO_CAPACITY_BODY,
        {
            "title": "Internal Server Error",
            "status": 500,
            "detail": "No instances available",
        },
    ],
)
def test_explicit_no_capacity_create_is_classified(body):
    client = RunpodREST(lambda: "fixture", opener=Refusing(500, body))
    with pytest.raises(CapacityError):
        client.create_pod({"name": "x"})


def test_other_create_failures_stay_uncertain_and_messages_are_policy():
    other = {"error": "internal error", "status": 500}
    client = RunpodREST(lambda: "fixture", opener=Refusing(500, other))
    with pytest.raises(ProviderError) as raised:
        client.create_pod({"name": "x"})
    assert not isinstance(raised.value, CapacityError)
    custom = RunpodREST(
        lambda: "fixture",
        opener=Refusing(500, other),
        no_capacity_messages=["internal error"],
    )
    with pytest.raises(CapacityError):
        custom.create_pod({"name": "x"})
    # Reads never classify as capacity.
    with pytest.raises(ProviderError) as raised:
        RunpodREST(
            lambda: "fixture", opener=Refusing(500, NO_CAPACITY_BODY)
        ).list_pods()
    assert not isinstance(raised.value, CapacityError)
