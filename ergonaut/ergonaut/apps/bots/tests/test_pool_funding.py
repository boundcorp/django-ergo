"""Funding is deliberate authenticated consent, separate from config and reconciliation."""

from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django_ergo.conversation import spend_guard
from django_ergo.conversation.models import SpendControlAudit, SpendFunding

from ergonaut.apps.bots.tests.test_pools_api import approve, captured_authority, kitchen, login, send  # noqa: F401

pytestmark = pytest.mark.django_db(transaction=True)


def test_owner_sees_pending_then_explicitly_approves_and_revises_without_reset(client, kitchen):  # noqa: F811 -- imported pytest fixture
    owner = login(client, "lee")
    row = next(p for p in client.get("/api/pools").json()["pools"] if p["name"] == "paid")
    assert row["funding_pending"] and row["grant"] is None
    details = client.get("/api/pools/paid/funding").json()
    assert details["pending"] and details["approval"] is None
    approved = approve(client, amount="2")
    grant = next(p for p in approved["pools"] if p["name"] == "paid")["grant"]
    spend_guard.reserve(
        grant_id=grant["id"],
        provider="openai",
        model="gpt-6.1-sol",
        amount=Decimal("2"),
        user_id=owner.pk,
        fields={},
        current=captured_authority(kitchen),
    )
    approve(client, amount="3")
    revised = client.get("/api/pools/paid/funding").json()
    assert Decimal(revised["reserved_usd"]) == 2
    assert Decimal(revised["approval"]["budget_usd"]) == 3
    assert SpendFunding.objects.filter(actor=owner).count() == 2
    assert SpendControlAudit.objects.filter(actor=owner, action="fund").count() == 2


@pytest.mark.parametrize(
    "change",
    [
        {"budget_usd": "NaN"},
        {"budget_usd": True},
        {"budget_usd": "0"},
        {"budget_usd": "0.00005"},
        {"accounts": []},
        {"accounts": ["invented-account"]},
        {"expires_at": "2099-01-01T00:00:00"},
        {"expires_at": "2020-01-01T00:00:00Z"},
        {"surprise": "authority"},
    ],
)
def test_invalid_funding_never_creates_consent(client, kitchen, change):  # noqa: F811 -- imported pytest fixture
    login(client, "lee")
    details = client.get("/api/pools/paid/funding").json()
    body = {
        "budget_usd": "3",
        "expires_at": "2099-01-01T00:00:00Z",
        "accounts": [a["identity"] for a in details["accounts"]],
        **change,
    }
    assert send(client, "post", "/api/pools/paid/funding", body).status_code in (400, 422)
    assert not SpendFunding.objects.exists()


def test_other_user_and_grantee_cannot_read_funding_or_approve_it(client, kitchen):  # noqa: F811 -- imported pytest fixture
    login(client, "lee")
    approve(client)
    friend = get_user_model().objects.create_user("friend", password="pw")
    issued = send(
        client,
        "post",
        "/api/pools/paid/grants",
        {"grantee": "friend", "budget_usd": "1", "expires_at": "2099-01-01T00:00:00Z"},
    )
    assert issued.status_code == 200
    client.logout()
    assert (
        client.post(
            "/api/auth/login", '{"username":"friend","password":"pw"}', content_type="application/json"
        ).status_code
        == 200
    )
    assert client.get("/api/pools/paid/funding").status_code == 403
    assert client.get("/api/pools/paid/envelope").status_code == 403
    assert (
        send(
            client,
            "post",
            "/api/pools/paid/funding",
            {"budget_usd": "9", "expires_at": "2099-01-01T00:00:00Z", "accounts": []},
        ).status_code
        == 403
    )
    seen = next(p for p in client.get("/api/pools").json()["pools"] if p["name"] == "paid")
    assert seen["configured_ceiling_usd"] is None
    assert seen["grant"]["grantee"] == friend.pk


def test_pause_requires_admin_and_retains_consent(client, kitchen):  # noqa: F811 -- imported pytest fixture
    login(client, "lee")
    approve(client)
    assert send(client, "post", "/api/pools/paid-pause", {"paused": True, "version": ""}).status_code == 403
    client.logout()
    admin = login(client, "root", admin=True)
    assert send(client, "post", "/api/pools/paid-pause", {"paused": "false", "version": ""}).status_code == 422
    version = client.get("/api/pools").json()["paid_pause_version"]
    assert send(client, "post", "/api/pools/paid-pause", {"paused": True}).status_code == 422
    paused = send(client, "post", "/api/pools/paid-pause", {"paused": True, "version": version})
    assert paused.status_code == 200 and paused.json()["paid_paused"]
    assert SpendFunding.objects.count() == 1
    assert SpendControlAudit.objects.filter(actor=admin, action="paid_pause").exists()
    assert send(client, "post", "/api/pools/paid-pause", {"paused": False, "version": version}).status_code == 409
    assert client.get("/api/pools").json()["paid_paused"]
    assert SpendControlAudit.objects.filter(action="paid_pause").count() == 1
    resumed = send(
        client, "post", "/api/pools/paid-pause", {"paused": False, "version": paused.json()["paid_pause_version"]}
    )
    assert resumed.status_code == 200 and not resumed.json()["paid_paused"]
