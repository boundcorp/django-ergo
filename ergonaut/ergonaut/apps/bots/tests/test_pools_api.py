"""The model pools API (ergonaut/api/pools.py): settings, version-checked writes,
owner/admin pool management and grants, the chat pool picker and listing
parity with what the runtime selects (django_ergo.bots.pools)."""

import json
import textwrap

import pytest
from django.contrib.auth import get_user_model
from django_ergo.bots import pools, routing, webhooks
from django_ergo.bots.providers import parse_providers

from ergonaut.apps.bots.loading import load_registry
from ergonaut.apps.bots.tests.fakes import fake_registry, say

pytestmark = pytest.mark.django_db(transaction=True)

BOT = """
name: kitchen
description: Runs the kitchen
orchestration: false
"""

PROVIDERS = """
default: auto/medium
providers:
  claude:
    type: claude
    transport: cli
    models: [claude-opus-5-5]
  openai:
    type: openai
    api_key_env: TEST_POOLS_OPENAI_KEY
    models:
      - {name: gpt-6.1-sol, price: {input: 1, output: 10}}
tiers:
  medium: [claude/claude-opus-5-5]
pools:
  - name: subs
    label: Subscriptions
    routes: [claude]
  - name: paid
    label: Paid API
    owner: lee
    routes: [openai]
    budget_usd: 5
    expires: 2099-12-31
    tiers: {medium: [openai/gpt-6.1-sol]}
"""


@pytest.fixture
def kitchen(tmp_path, monkeypatch):
    folder = tmp_path / "kitchen"
    folder.mkdir()
    (folder / "agents.md").write_text("You run the kitchen.")
    (folder / "bot.yaml").write_text(textwrap.dedent(BOT))
    registry, _ = fake_registry(folder, say("hi"))
    registry.providers = parse_providers(PROVIDERS)
    webhooks.set_registry(registry)
    monkeypatch.setenv("TEST_POOLS_OPENAI_KEY", "sk-test")
    monkeypatch.setattr("django_ergo.conversation.engines.claude_code.claude_installed", lambda command="": True)
    monkeypatch.setattr(routing, "current_usage", lambda now=None: {})
    yield registry
    webhooks.set_registry(load_registry)


def login(client, name, *, admin=False):
    model = get_user_model()
    user = (model.objects.create_superuser if admin else model.objects.create_user)(name, f"{name}@example.com", "pw")
    response = client.post(
        "/api/auth/login", json.dumps({"username": name, "password": "pw"}), content_type="application/json"
    )
    assert response.status_code == 200
    return user


def send(client, method, url, data):
    return getattr(client, method)(url, json.dumps(data), content_type="application/json")


def approve(client, pool="paid", amount="5"):
    """Explicit mock consent through the same endpoint the UI uses."""
    details = client.get(f"/api/pools/{pool}/funding")
    assert details.status_code == 200, details.content
    response = send(
        client,
        "post",
        f"/api/pools/{pool}/funding",
        {
            "budget_usd": amount,
            "expires_at": "2099-12-31T00:00:00Z",
            "accounts": [a["identity"] for a in details.json()["accounts"]],
            "note": "Explicit synthetic API test consent",
        },
    )
    assert response.status_code == 200, response.content
    return response.json()


def test_settings_list_pools_and_owners_change_them(client, kitchen):
    login(client, "lee", admin=True)
    approve(client)
    data = client.get("/api/pools").json()
    assert [p["name"] for p in data["pools"]] == ["subs", "paid"]
    assert data["active"] == "subs" and data["auto_rollover"] is False and data["editable"]
    paid = data["pools"][1]
    assert (paid["status"], paid["rollover_out"], paid["allow_auto_entry"]) == ("eligible", False, False)
    assert paid["is_owner"] and paid["manageable"] and paid["grant"]["budget_usd"] == "5.0000"
    changed = send(client, "patch", "/api/pools/paid", {"version": paid["version"], "allow_auto_entry": True}).json()
    assert changed["pools"][1]["allow_auto_entry"] is True
    # A stale version is a conflict, not a silent overwrite; a missing one is refused.
    stale = send(client, "patch", "/api/pools/paid", {"version": paid["version"], "enabled": False})
    assert stale.status_code == 409
    assert send(client, "patch", "/api/pools/paid", {"enabled": False}).status_code == 422
    # Budgets are grants now, not pool settings.
    assert send(client, "patch", "/api/pools/paid", {"version": 1, "budget_usd": "7.5"}).status_code == 422
    provider = next(p for p in data["providers"] if p["name"] == "openai")
    assert send(client, "patch", "/api/providers/openai", {"enabled": False}).status_code == 422
    off = send(client, "patch", "/api/providers/openai", {"enabled": False, "version": provider["version"]}).json()
    assert next(p for p in off["providers"] if p["name"] == "openai")["enabled"] is False


def test_people_change_their_own_choices_but_not_others_pools(client, kitchen):
    login(client, "cook")
    data = client.get("/api/pools").json()
    assert data["editable"] is False
    # lee's paid pool isn't visible to cook at all.
    assert [p["name"] for p in data["pools"]] == ["subs"]
    assert send(client, "patch", "/api/pools/paid", {"version": 0, "enabled": False}).status_code == 403
    assert send(client, "patch", "/api/providers/openai", {"enabled": False, "version": 0}).status_code == 403
    assert send(client, "patch", "/api/pools/preferences", {"version": 0, "active_pool": "paid"}).status_code == 403
    mine = send(client, "patch", "/api/pools/preferences", {"version": 0, "auto_rollover": True}).json()
    assert mine["auto_rollover"] is True
    bad = send(client, "patch", "/api/pools/preferences", {"version": mine["user_version"], "order": ["paid"]})
    assert bad.status_code == 400
    # Reordering lists only the pools cook can see (what the UI sends).
    ordered = send(client, "patch", "/api/pools/preferences", {"version": mine["user_version"], "order": ["subs"]})
    assert ordered.status_code == 200
    assert pools.preference(get_user_model().objects.get(username="cook").pk).order == ["subs"]


def test_config_edits_revise_the_owners_grant_without_resetting_its_spend(client, kitchen):
    """providers.yaml budget edits are revisions of one spending envelope: the Pools
    page shows the new budget with the same spend; removing the budget revokes it."""
    from decimal import Decimal

    from django_ergo.conversation import spend_guard

    lee = login(client, "lee")
    approve(client)
    first = next(p for p in client.get("/api/pools").json()["pools"] if p["name"] == "paid")["grant"]
    assert first["budget_usd"] == "5.0000"
    spend_guard.reserve(
        grant_id=first["id"],
        provider="openai",
        model="gpt-6.1-sol",
        amount=Decimal("1.5"),
        user_id=lee.pk,
        fields={},
        current=pools.SendAuthority(kitchen),
    )
    kitchen.providers = parse_providers(PROVIDERS.replace("budget_usd: 5", "budget_usd: 50"))
    raised = next(p for p in client.get("/api/pools").json()["pools"] if p["name"] == "paid")
    assert raised["grant"]["budget_usd"] == "5.0000" and raised["grant"]["id"] != first["id"]
    assert Decimal(raised["grant"]["reserved_usd"]) == Decimal("1.5")
    kitchen.providers = parse_providers(PROVIDERS.replace("    budget_usd: 5\n", ""))
    removed = next(p for p in client.get("/api/pools").json()["pools"] if p["name"] == "paid")
    assert removed["grant"] is None and removed["status"] == "budget_required"


def test_chat_pool_picker_switch_listing_and_refusal(client, kitchen, monkeypatch):
    login(client, "lee")
    approve(client)
    root = send(client, "post", "/api/bots/kitchen/root", {}).json()
    view = client.get(f"/api/sessions/{root['id']}/pool").json()
    assert (view["active"], view["chat_pool"], view["refusal"]) == ("subs", "", None)
    models = client.get(f"/api/bots/kitchen/models?session={root['id']}").json()
    assert [m["id"] for m in models["models"] if m["provider"] != "auto"] == ["claude/claude-opus-5-5"]
    assert "auto/medium" in [m["id"] for m in models["models"]]
    # Out-of-pool pins are refused.
    assert send(client, "post", f"/api/sessions/{root['id']}/model", {"model": "openai/gpt-6.1-sol"}).status_code == 409
    switched = send(
        client, "post", f"/api/sessions/{root['id']}/pool", {"pool": "paid", "scope": "chat", "chat_version": 0}
    ).json()
    assert switched["session"]["pool"] == "paid" and switched["warning"] == ""
    assert switched["session"]["pool_version"] == 1
    models = client.get(f"/api/bots/kitchen/models?session={root['id']}").json()
    assert [m["id"] for m in models["models"] if m["provider"] != "auto"] == ["openai/gpt-6.1-sol"]
    assert models["models"][0]["billing"] == "metered"
    # A stale chat version is a conflict.
    stale = send(client, "post", f"/api/sessions/{root['id']}/pool", {"pool": "", "scope": "chat", "chat_version": 0})
    assert stale.status_code == 409
    # The same eligibility the runtime selects with: a chat stopped on exhaustion shows next choices.
    send(client, "post", f"/api/sessions/{root['id']}/pool", {"pool": "", "scope": "chat", "chat_version": 1})
    monkeypatch.setattr(routing, "current_usage", lambda now=None: {"claude": {"weekly": 100}})
    from django_ergo.conversation.models import ConversationSession

    session = ConversationSession.objects.get(pk=root["id"])
    with pytest.raises(pools.PoolRefusal):
        webhooks.get_registry().get("kitchen").route(session)
    stopped = client.get(f"/api/sessions/{root['id']}/pool").json()["refusal"]
    assert stopped["code"] == "pool_exhausted"
    assert [c["name"] for c in stopped["next_choices"]] == ["paid"]
    detail = client.get(f"/api/sessions/{root['id']}").json()
    assert detail["session"]["pool_refusal"]["code"] == "pool_exhausted"
    # The picker shows the exhausted model as configured but not selectable now.
    models = client.get(f"/api/bots/kitchen/models?session={root['id']}").json()
    claude = next(m for m in models["models"] if m["id"] == "claude/claude-opus-5-5")
    assert claude["configured"] and not claude["available"] and claude["status"] == "no_capacity"
    tier = next(m for m in models["models"] if m["id"] == "auto/medium")
    assert not tier["available"]
    # Switching clears it.
    send(client, "post", f"/api/sessions/{root['id']}/pool", {"pool": "paid", "scope": "chat", "chat_version": 2})
    assert client.get(f"/api/sessions/{root['id']}/pool").json()["refusal"] is None


def test_user_default_from_a_chat(client, kitchen):
    user = login(client, "lee")
    approve(client)
    root = send(client, "post", "/api/bots/kitchen/root", {}).json()
    out = send(
        client,
        "post",
        f"/api/sessions/{root['id']}/pool",
        {"pool": "paid", "scope": "user", "version": 0, "chat_version": 0},
    ).json()
    assert out["session"]["pool"] == ""
    assert pools.preference(user.pk).active_pool == "paid"
    assert client.get("/api/pools").json()["selected"] == "paid"


def test_only_an_admin_links_a_pool_on_already_funded_accounts(client, kitchen):
    """A second owned pool on the OpenAI account paid already funds gets no
    budget from config sync. Its owner can't link it; an admin sees the
    overlapping envelope and funds it separately (or continues one)."""
    extra = PROVIDERS + (
        "  - name: extra\n    owner: lee\n    routes: [openai]\n    budget_usd: 50\n    expires: 2099-12-31\n"
    )
    kitchen.providers = parse_providers(extra)
    login(client, "lee")
    approve(client)
    rows = {p["name"]: p for p in client.get("/api/pools").json()["pools"]}
    assert rows["paid"]["grant"] is not None
    assert rows["extra"]["grant"] is None
    assert client.get("/api/pools/extra/envelope").status_code == 403
    assert send(client, "post", "/api/pools/extra/envelope", {"fund": True}).status_code == 403
    client.logout()
    login(client, "root", admin=True)
    found = client.get("/api/pools/extra/envelope").json()
    assert "an admin must link" in found["problem"]
    (envelope,) = found["envelopes"]
    assert envelope["pool"] == "paid" and envelope["in_use"] and not envelope["continuable"]
    used = send(client, "post", "/api/pools/extra/envelope", {"envelope": envelope["id"]})
    assert used.status_code == 400 and "still uses" in used.json()["detail"]
    assert send(client, "post", "/api/pools/extra/envelope", {}).status_code == 400
    assert send(client, "post", "/api/pools/extra/envelope", {"fund": True, "note": "own $50"}).status_code == 200
    assert client.get("/api/pools/extra/envelope").json()["problem"] == ""
    assert client.get("/api/pools/extra/funding").json()["pending"]
    approve(client, pool="extra", amount="50")
    # The owner gives the authority up; only an admin's explicit restore lifts it.
    lee = get_user_model().objects.get(username="lee")
    own = pools.snapshot(kitchen.providers, lee.pk).get("extra").grant
    pools.revoke_grant(kitchen.providers, own.pk, by_id=lee.pk)
    assert send(client, "post", "/api/pools/extra/envelope", {"fund": True}).status_code == 200
    assert pools.snapshot(kitchen.providers, lee.pk).get("extra").grant is None
    assert send(client, "post", "/api/pools/extra/envelope", {"restore": True}).status_code == 200
    lee = get_user_model().objects.get(username="lee")
    own = pools.snapshot(kitchen.providers, lee.pk).get("extra")
    assert own.grant is not None and own.grant.budget_usd == 50
