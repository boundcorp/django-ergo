"""The model pools review's API reproducers (design/MODEL_POOLS_REVIEW.md),
inverted into acceptance tests: R2 (owned pools, grants), R15 (atomic scope
switch) and R17 (server-side amount and window validation)."""

import pytest
from django_ergo.bots import pools
from django_ergo.conversation.models import ConversationSession

from ergonaut.apps.bots.tests.test_pools_api import approve, kitchen, login, send  # noqa: F401

pytestmark = pytest.mark.django_db(transaction=True)


def test_r2_nonadmin_cannot_activate_another_users_paid_authority(client, kitchen):  # noqa: F811
    user = login(client, "stranger")
    response = send(client, "patch", "/api/pools/preferences", {"version": 0, "active_pool": "paid"})
    assert response.status_code == 403
    root = send(client, "post", "/api/bots/kitchen/root", {}).json()
    chat = send(
        client, "post", f"/api/sessions/{root['id']}/pool", {"pool": "paid", "scope": "chat", "chat_version": 0}
    )
    assert chat.status_code == 403
    session = ConversationSession.objects.get(pk=root["id"])
    decision = pools.select_route(kitchen.get("kitchen"), session, ref="auto/medium")
    assert not decision.metered and decision.pool == "subs"
    assert pools.preference(user.pk) is None or pools.preference(user.pk).active_pool == ""
    # No other user's spend in anything they can read.
    assert "paid" not in str(client.get("/api/pools").json()["pools"])


def test_r2_owner_issues_a_scoped_grant_and_the_grantee_sees_only_theirs(client, kitchen):  # noqa: F811
    from django.contrib.auth import get_user_model

    friend = get_user_model().objects.create_user("friend", "friend@example.com", "pw")
    login(client, "lee")
    approve(client)
    # Only the owner issues; a grant needs an end date with a timezone.
    missing = send(client, "post", "/api/pools/paid/grants", {"grantee": "friend", "budget_usd": "3"})
    assert missing.status_code == 422
    naive = send(
        client,
        "post",
        "/api/pools/paid/grants",
        {"grantee": "friend", "budget_usd": "3", "expires_at": "2099-01-01T00:00"},
    )
    assert naive.status_code == 400
    issued = send(
        client,
        "post",
        "/api/pools/paid/grants",
        {"grantee": "friend", "budget_usd": "3", "expires_at": "2099-01-01T00:00:00Z"},
    ).json()
    paid = next(p for p in issued["pools"] if p["name"] == "paid")
    assert [g["grantee"] for g in paid["grants"]] == [friend.pk]
    client.post("/api/auth/logout")
    response = client.post(
        "/api/auth/login", '{"username": "friend", "password": "pw"}', content_type="application/json"
    )
    assert response.status_code == 200
    seen = next(p for p in client.get("/api/pools").json()["pools"] if p["name"] == "paid")
    assert seen["grant"]["budget_usd"] == "3.0000" and seen["grants"] == []
    assert not seen["manageable"]
    assert (
        send(
            client,
            "post",
            "/api/pools/paid/grants",
            {"grantee": "friend", "budget_usd": "99", "expires_at": "2099-01-01T00:00:00Z"},
        ).status_code
        == 403
    )
    assert send(client, "patch", "/api/pools/preferences", {"version": 0, "active_pool": "paid"}).status_code == 200


@pytest.mark.parametrize("amount", ["-2", "NaN", "Infinity", "1e400", "lots"])
def test_r17_grant_amounts_are_checked_on_the_server(client, kitchen, amount):  # noqa: F811
    login(client, "lee")
    approve(client)
    for field in ("budget_usd", "per_launch_usd"):
        body = {"grantee": "lee", "budget_usd": "5", "expires_at": "2099-01-01T00:00:00Z", field: amount}
        response = send(client, "post", "/api/pools/paid/grants", body)
        assert response.status_code == 400, (field, amount, response.content)


def test_r15_cross_scope_switch_is_atomic(client, kitchen, monkeypatch):  # noqa: F811
    user = login(client, "partial")
    root = send(client, "post", "/api/bots/kitchen/root", {}).json()
    assert (
        send(
            client, "post", f"/api/sessions/{root['id']}/pool", {"pool": "subs", "scope": "chat", "chat_version": 0}
        ).status_code
        == 200
    )
    # A stale chat version fails the whole switch: the user default is unchanged.
    result = send(
        client,
        "post",
        f"/api/sessions/{root['id']}/pool",
        {"pool": "", "scope": "user", "version": 0, "chat_version": 0},
    )
    assert result.status_code == 409
    assert pools.preference(user.pk) is None
    session = ConversationSession.objects.get(pk=root["id"])
    assert session.metadata[pools.SESSION_POOL_KEY] == "subs"
    # And a failure while clearing the chat override rolls the default back.
    original = ConversationSession.save

    def fail(self, *args, **kwargs):
        if self.pk == session.pk:
            raise ValueError("mock failure clearing chat override")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(ConversationSession, "save", fail)
    result = send(
        client,
        "post",
        f"/api/sessions/{root['id']}/pool",
        {"pool": "", "scope": "user", "version": 0, "chat_version": 1},
    )
    assert result.status_code == 400
    monkeypatch.setattr(ConversationSession, "save", original)
    assert pools.preference(user.pk) is None
    session.refresh_from_db()
    assert session.metadata[pools.SESSION_POOL_KEY] == "subs"
