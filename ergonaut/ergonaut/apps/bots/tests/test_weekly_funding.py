"""Authenticated recurring terms, vendor mappings and optimistic writes."""

import json

import pytest
from django.contrib.auth import get_user_model
from django_ergo.conversation.models import ProviderAccountMapping, ProviderWeeklyConsent

from ergonaut.apps.bots.tests.test_pools_api import kitchen, login, send  # noqa: F401

pytestmark = pytest.mark.django_db(transaction=True)


def put(client, path, body):
    return client.put(path, json.dumps(body), content_type="application/json")


def test_weekly_terms_require_admin_mapping_owner_consent_and_versions(client, kitchen):  # noqa: F811
    owner = login(client, "lee")
    details = client.get("/api/pools/paid/funding").json()
    body = {
        "identity": details["accounts"][0]["identity"],
        "owner": "lee",
        "account_label": "Synthetic OpenAI account",
        "assertion": "Admin asserts this OpenAI billing account",
    }
    assert send(client, "post", "/api/pools/weekly-mapping", body).status_code == 403
    client.logout()
    admin = login(client, "root", admin=True)
    mapped = send(client, "post", "/api/pools/weekly-mapping", body)
    assert mapped.status_code == 200
    assert mapped.json()["owner_id"] == str(owner.pk)
    assert mapped.json()["version"] == 0 and not mapped.json()["enabled"]
    assert ProviderAccountMapping.objects.get().actor_id == admin.pk
    assert not ProviderWeeklyConsent.objects.exists()
    client.logout()
    assert (
        client.post(
            "/api/auth/login", '{"username":"lee","password":"pw"}', content_type="application/json"
        ).status_code
        == 200
    )
    terms = {
        "version": 0,
        "weekly_usd": "100",
        "enabled": True,
        "valid_from": "2026-01-01T00:00:00Z",
        "expires_at": "2099-01-01T00:00:00Z",
        "timezone": "UTC",
        "note": "Explicit synthetic recurring consent",
    }
    response = put(client, "/api/pools/weekly/openai", terms)
    assert response.status_code == 200
    assert response.json()["version"] == 1
    assert response.json()["actor_id"] == str(owner.pk)
    assert put(client, "/api/pools/weekly/openai", terms).status_code == 409
    assert ProviderWeeklyConsent.objects.count() == 1
    funding = {
        "version": details["version"],
        "budget_period": "iso_week_utc",
        "budget_usd": "100",
        "expires_at": "2099-01-01T00:00:00Z",
        "accounts": [body["identity"]],
    }
    assert send(client, "post", "/api/pools/paid/funding", funding).status_code == 200
    assert send(client, "post", "/api/pools/paid/funding", funding).status_code == 409
    revised = client.get("/api/pools/paid/funding").json()
    assert revised["budget_period"] == "iso_week_utc" and revised["timezone"] == "UTC"
    assert revised["approval"]["actor_id"] == owner.pk
    assert put(client, "/api/pools/weekly/openai", {**terms, "version": 1, "enabled": False}).status_code == 200


@pytest.mark.parametrize(
    "change",
    [
        {"weekly_usd": "100.0001"},
        {"weekly_usd": "NaN"},
        {"weekly_usd": "-1"},
        {"timezone": "America/Los_Angeles"},
        {"enabled": "true"},
        {"valid_from": "2026-01-01T00:00:00"},
        {"surprise": True},
    ],
)
def test_invalid_monthly_terms_never_create_consent(client, kitchen, change):  # noqa: F811
    owner = login(client, "lee")
    client.logout()
    login(client, "root", admin=True)
    identity = client.get("/api/pools/paid/funding").json()["accounts"][0]["identity"]
    assert (
        send(
            client,
            "post",
            "/api/pools/weekly-mapping",
            {
                "identity": identity,
                "owner": owner.username,
                "account_label": "Synthetic",
                "assertion": "Explicit test assertion",
            },
        ).status_code
        == 200
    )
    body = {
        "version": 0,
        "weekly_usd": "100",
        "enabled": True,
        "valid_from": "2026-01-01T00:00:00Z",
        "expires_at": "2099-01-01T00:00:00Z",
        "timezone": "UTC",
        "note": "Synthetic test consent",
        **change,
    }
    assert put(client, "/api/pools/weekly/openai", body).status_code in (400, 422)
    assert not ProviderWeeklyConsent.objects.exists()


def test_other_users_cannot_inspect_or_change_global_provider_terms(client, kitchen):  # noqa: F811
    owner = login(client, "lee")
    client.logout()
    login(client, "root", admin=True)
    identity = client.get("/api/pools/paid/funding").json()["accounts"][0]["identity"]
    assert (
        send(
            client,
            "post",
            "/api/pools/weekly-mapping",
            {
                "identity": identity,
                "owner": owner.username,
                "account_label": "Synthetic",
                "assertion": "Explicit test assertion",
            },
        ).status_code
        == 200
    )
    client.logout()
    login(client, "outsider")
    assert client.get("/api/pools/weekly/openai").status_code == 403
    client.logout()
    assert client.get("/api/pools/weekly/openai").status_code == 401
    assert get_user_model().objects.count() == 3


def _fund_monthly(client, kitchen):  # noqa: F811
    owner = login(client, "lee")
    details = client.get("/api/pools/paid/funding").json()
    identity = details["accounts"][0]["identity"]
    client.logout()
    login(client, "root", admin=True)
    body = {
        "identity": identity,
        "owner": "lee",
        "account_label": "Synthetic OpenAI account",
        "assertion": "Admin asserts this OpenAI billing account",
    }
    assert send(client, "post", "/api/pools/weekly-mapping", body).status_code == 200
    client.logout()
    assert (
        client.post(
            "/api/auth/login", '{"username":"lee","password":"pw"}', content_type="application/json"
        ).status_code
        == 200
    )
    funding = {
        "version": details["version"],
        "budget_period": "iso_week_utc",
        "budget_usd": "100",
        "expires_at": "2099-01-01T00:00:00Z",
        "accounts": [identity],
    }
    assert send(client, "post", "/api/pools/paid/funding", funding).status_code == 200
    return owner, identity


def test_funding_write_requires_version_and_period_and_never_converts_silently(client, kitchen):  # noqa: F811
    from django_ergo.conversation.models import SpendFunding

    _, identity = _fund_monthly(client, kitchen)
    current = client.get("/api/pools/paid/funding").json()
    base = {"budget_usd": "100", "expires_at": "2099-01-01T00:00:00Z", "accounts": [identity]}
    count = SpendFunding.objects.count()
    # No version, or no period (the pre-fix defaults): refused, nothing written.
    for body in (
        {**base, "budget_period": "iso_week_utc"},
        {**base, "version": current["version"]},
        {**base},
    ):
        assert send(client, "post", "/api/pools/paid/funding", body).status_code in (400, 422)
    # A cumulative write on a weekly approval is a conversion and must be explicit.
    silent = {**base, "budget_period": "cumulative", "version": current["version"]}
    assert send(client, "post", "/api/pools/paid/funding", silent).status_code == 400
    assert SpendFunding.objects.count() == count
    stale = {**base, "budget_period": "iso_week_utc", "version": current["version"] - 1}
    assert send(client, "post", "/api/pools/paid/funding", stale).status_code == 409
    assert client.get("/api/pools/paid/funding").json()["budget_period"] == "iso_week_utc"
    # The explicit, audited conversion works once.
    converted = {**silent, "convert_period": True}
    assert send(client, "post", "/api/pools/paid/funding", converted).status_code == 200
    assert client.get("/api/pools/paid/funding").json()["budget_period"] == "cumulative"
