"""Independent mock-only final review: acceptance inversions of C1-C4."""

import pytest
from django.contrib.auth import get_user_model

from django_ergo.bots import pools
from django_ergo.bots.providers import current_providers
from django_ergo.conversation import spend_guard
from django_ergo.conversation.models import SpendEnvelope
from django_ergo.conversation.models import SpendGrant
from tests.pool_funding import fund
from tests.test_model_pools import POOLS
from tests.test_model_pools import chat
from tests.test_model_pools import (
    environment,  # noqa: F401 -- synthetic keys/CLIs for this module
)
from tests.test_model_pools_authority import _process
from tests.test_model_pools_rereview import _guard
from tests.test_model_pools_rereview import _send

User = get_user_model()


@pytest.mark.django_db
def test_initial_ceiling_cannot_create_unconsented_spend_authority(tmp_path):
    user = User.objects.create(username="lee")
    (tmp_path / "providers.yaml").write_text(POOLS)
    _, bot = _process(tmp_path)
    assert not SpendEnvelope.objects.exists()
    with pytest.raises(pools.PoolRefusal):
        _guard(bot, user)
    assert not SpendGrant.objects.filter(source="config").exists()
    assert SpendEnvelope.objects.get().linked_by_id is None
    fund(bot.providers, user)
    _send(_guard(bot, user))


@pytest.mark.django_db
def test_built_guard_denies_new_privacy_restriction(tmp_path):
    user = User.objects.create(username="lee")
    path = tmp_path / "providers.yaml"
    path.write_text(POOLS)
    registry, bot = _process(tmp_path)
    fund(bot.providers, user)
    guard = _guard(bot, user)
    path.write_text(POOLS + "\nprivacy:\n  work:\n    bots: [devbox]\n    allow: []\n")
    # A fully refreshed selector denies the same route.
    registry.providers = current_providers(bot.providers)
    with pytest.raises(pools.PoolRefusal):
        pools.select_route(bot, chat(bot, user), ref="openai/gpt-6.1-sol")
    with pytest.raises(spend_guard.SpendDenied, match="privacy"):
        _send(guard)


@pytest.mark.django_db
@pytest.mark.parametrize("change", ["empty", "remove", "disabled", "model_removed"])
def test_existing_guard_denies_removed_authority(tmp_path, change):
    user = User.objects.create(username="lee")
    path = tmp_path / "providers.yaml"
    path.write_text(POOLS)
    _, bot = _process(tmp_path)
    fund(bot.providers, user)
    guard = _guard(bot, user)
    if change == "remove":
        path.unlink()
    elif change == "empty":
        path.write_text("")
    elif change == "disabled":
        path.write_text(POOLS.replace("  openai:\n", "  openai:\n    enabled: false\n"))
    else:
        path.write_text(
            POOLS.replace(
                "      - {name: gpt-6.1-sol, price: {input: 1, cached: 0.1, output: 10}}\n",
                "",
            )
        )
    with pytest.raises(spend_guard.SpendDenied):
        _send(guard)


@pytest.mark.django_db(transaction=True)
def test_built_google_engine_refuses_account_change(tmp_path, monkeypatch):
    import httpx
    from asgiref.sync import async_to_sync

    google_api = pytest.importorskip("django_ergo.conversation.engines.google_api")
    from tests.test_conversation_google_spend import POOLS as GOOGLE_POOLS
    from tests.test_conversation_google_spend import reply

    monkeypatch.setenv("TEST_GEMINI_KEY", "fake-old-account")
    monkeypatch.setenv("REVIEW_NEW_GEMINI_KEY", "fake-new-account")
    user = User.objects.create(username="lee")
    path = tmp_path / "providers.yaml"
    path.write_text(GOOGLE_POOLS)
    _, bot = _process(tmp_path)
    fund(bot.providers, user)
    session = chat(bot, user)
    pools.update_preference(user.pk, bot.providers, version=0, active_pool="paid")
    decision = pools.select_route(bot, session, ref="gemini/gemini-2.5-pro")
    engine = google_api.GoogleAPIEngine(
        {"model": "gemini-2.5-pro", "api_key_env": "TEST_GEMINI_KEY"}
    )
    engine.spend_guard = decision.guard(bot, session)
    requests = []

    def send(request):
        requests.append(request)
        return httpx.Response(200, json=reply())

    monkeypatch.setattr(google_api, "_transport", lambda: httpx.MockTransport(send))
    path.write_text(GOOGLE_POOLS.replace("TEST_GEMINI_KEY", "REVIEW_NEW_GEMINI_KEY"))
    assert (
        current_providers(bot.providers).providers["gemini"].api_key_env
        == "REVIEW_NEW_GEMINI_KEY"
    )
    with pytest.raises(spend_guard.SpendDenied, match="identity"):
        async_to_sync(engine.complete)([{"role": "user", "content": "hello"}])
    assert requests == []


@pytest.mark.django_db
def test_remove_pools_cannot_drop_budget_guard(tmp_path):
    from tests.test_model_pools_authority import _hold

    user = User.objects.create(username="lee")
    path = tmp_path / "providers.yaml"
    path.write_text(POOLS)
    registry, bot = _process(tmp_path)
    fund(bot.providers, user)
    old_guard = _guard(bot, user)
    own = SpendGrant.objects.get(pk=old_guard.grant_id)
    _hold(own, user, "10", bot.providers)  # all money held, unknown outcomes
    with pytest.raises(spend_guard.SpendDenied):
        _send(old_guard)
    path.write_text(
        POOLS[: POOLS.index("\npools:")] + POOLS[POOLS.index("\nprivate_pools:") :]
    )
    registry.providers = current_providers(bot.providers)
    session = chat(bot, user)
    with pytest.raises(pools.PoolRefusal):
        pools.select_route(bot, session, ref="openai/gpt-6.1-sol")


@pytest.mark.django_db
def test_config_ceiling_growth_cannot_widen_explicit_funding(tmp_path):
    from decimal import Decimal

    user = User.objects.create(username="lee")
    path = tmp_path / "providers.yaml"
    path.write_text(POOLS)
    registry, bot = _process(tmp_path)
    fund(bot.providers, user, amount="3")
    original = _guard(bot, user)
    path.write_text(POOLS.replace("budget_usd: 10", "budget_usd: 1000"))
    registry.providers = current_providers(bot.providers)
    current = pools.snapshot(bot.providers, user.pk).get("paid").grant
    assert current.budget_usd == Decimal(3)
    assert (
        current.envelope_id == SpendGrant.objects.get(pk=original.grant_id).envelope_id
    )


@pytest.mark.django_db
def test_funding_revision_keeps_unknown_holds_and_audits_actor(tmp_path):
    from django_ergo.conversation.models import SpendControlAudit
    from django_ergo.conversation.models import SpendFunding
    from tests.test_model_pools_authority import _hold

    user = User.objects.create(username="lee")
    (tmp_path / "providers.yaml").write_text(POOLS)
    _, bot = _process(tmp_path)
    first = fund(bot.providers, user, amount="2")
    guard = _guard(bot, user)
    _hold(SpendGrant.objects.get(pk=guard.grant_id), user, "2", bot.providers)
    second = fund(bot.providers, user, amount="3")
    assert first.envelope_id == second.envelope_id
    view = pools.snapshot(bot.providers, user.pk).get("paid")
    assert view.reserved_usd == 2 and view.grant.budget_usd == 3
    assert SpendFunding.objects.filter(envelope=first.envelope, actor=user).count() == 2
    assert (
        SpendControlAudit.objects.filter(
            envelope=first.envelope, action="fund", actor=user
        ).count()
        == 2
    )
    with pytest.raises(spend_guard.SpendDenied):
        _hold(view.grant, user, "1.01", bot.providers)


@pytest.mark.django_db
@pytest.mark.parametrize(
    "bad", [None, "", True, "NaN", "-1", "0", "0.000000001", "100000000"]
)
def test_invalid_funding_never_approves(tmp_path, bad):
    user = User.objects.create(username="lee")
    (tmp_path / "providers.yaml").write_text(POOLS)
    _, bot = _process(tmp_path)
    with pytest.raises(ValueError, match="Funding|funding|configured accounts"):
        fund(bot.providers, user, amount=bad if bad is not None else "")
    from django_ergo.conversation.models import SpendFunding

    assert not SpendFunding.objects.exists()


@pytest.mark.django_db
def test_funding_requires_actual_owner_or_admin_and_explicit_accounts(tmp_path):
    user = User.objects.create(username="lee")
    other = User.objects.create(username="other")
    (tmp_path / "providers.yaml").write_text(POOLS)
    _, bot = _process(tmp_path)
    with pytest.raises(PermissionError):
        fund(bot.providers, other)
    with pytest.raises(PermissionError):
        pools.link_config_envelope(
            bot.providers, "paid", by_id=user.pk, admin=True, fund=True
        )
    config = bot.providers.pools.get("paid")
    with pytest.raises(ValueError, match="Funding|funding|configured accounts"):
        pools.approve_funding(
            bot.providers,
            "paid",
            by_id=user.pk,
            budget_usd="2",
            expires_at=config.expires_at,
            accounts=[],
        )


@pytest.mark.django_db
@pytest.mark.parametrize(
    "replacement",
    [
        ("TEST_POOL_OPENAI_KEY", "OTHER_KEY"),
        (
            "    api_key_env: TEST_POOL_OPENAI_KEY",
            "    account: other-account\n    api_key_env: TEST_POOL_OPENAI_KEY",
        ),
        (
            "    api_key_env: TEST_POOL_OPENAI_KEY",
            "    config: {base_url: 'https://other.example/v1'}\n    api_key_env: TEST_POOL_OPENAI_KEY",
        ),
        ("  openai:\n    type: openai", "  openai:\n    type: claude"),
        (
            "  openai:\n    type: openai",
            "  openai:\n    type: openai\n    transport: cli",
        ),
    ],
)
def test_prebuilt_guard_rejects_transport_identity_changes(tmp_path, replacement):
    user = User.objects.create(username="lee")
    path = tmp_path / "providers.yaml"
    path.write_text(POOLS)
    _, bot = _process(tmp_path)
    fund(bot.providers, user)
    guard = _guard(bot, user)
    path.write_text(POOLS.replace(*replacement))
    with pytest.raises(spend_guard.SpendDenied):
        _send(guard)


@pytest.mark.django_db
def test_queued_work_cannot_adopt_new_account(tmp_path):
    user = User.objects.create(username="lee")
    path = tmp_path / "providers.yaml"
    path.write_text(POOLS)
    registry, bot = _process(tmp_path)
    fund(bot.providers, user)
    _guard(bot, user)
    session = chat(bot, user)
    queued = pools.capture_policy(bot, session)
    path.write_text(POOLS.replace("TEST_POOL_OPENAI_KEY", "OTHER_KEY"))
    registry.providers = current_providers(bot.providers)
    with pools.policy_scope(queued), pytest.raises(pools.PoolRefusal):
        pools.select_route(bot, session, ref="openai/gpt-6.1-sol")


@pytest.mark.django_db
def test_prebuilt_guard_rechecks_session_origin_and_policy_disappearance(tmp_path):
    user = User.objects.create(username="lee")
    path = tmp_path / "providers.yaml"
    path.write_text(POOLS)
    _, bot = _process(tmp_path)
    fund(bot.providers, user)
    guard = _guard(bot, user)
    from django_ergo.conversation.models import ConversationSession

    session = ConversationSession.objects.get(pk=guard.session_id)
    session.metadata[pools.ORIGIN_KEY] = ["removed-policy"]
    session.save()
    with pytest.raises(spend_guard.SpendDenied, match="privacy"):
        _send(guard)


@pytest.mark.django_db
def test_paid_pause_preserves_grants_and_holds(tmp_path):
    from tests.test_model_pools_authority import _hold

    user = User.objects.create(username="lee")
    admin = User.objects.create(username="admin", is_superuser=True)
    (tmp_path / "providers.yaml").write_text(POOLS)
    _, bot = _process(tmp_path)
    fund(bot.providers, user)
    guard = _guard(bot, user)
    row = SpendGrant.objects.get(pk=guard.grant_id)
    _hold(row, user, "2", bot.providers)
    pools.set_paid_pause(by_id=admin.pk, paused=True)
    with pytest.raises(spend_guard.SpendDenied, match="paused"):
        _send(guard)
    assert spend_guard.spent_and_reserved(row.pk)[1] == 2
    row.refresh_from_db()
    assert row.revoked_at is None
    pools.set_paid_pause(by_id=admin.pk, paused=False)
    _send(guard)


@pytest.mark.django_db
def test_new_engine_cannot_adopt_an_account_without_new_consent(tmp_path):
    user = User.objects.create(username="lee")
    path = tmp_path / "providers.yaml"
    path.write_text(POOLS)
    registry, bot = _process(tmp_path)
    fund(bot.providers, user)
    _guard(bot, user)
    path.write_text(POOLS.replace("TEST_POOL_OPENAI_KEY", "TEST_POOL_ANTHROPIC_KEY"))
    registry.providers = current_providers(bot.providers)
    with pytest.raises(pools.PoolRefusal):
        _guard(bot, user)


@pytest.mark.django_db
def test_captured_restrictions_cannot_widen_and_current_origin_is_checked(tmp_path):
    from django_ergo.bots.pool_config import PrivacyPolicy

    user = User.objects.create(username="lee")
    path = tmp_path / "providers.yaml"
    path.write_text(POOLS)
    _, bot = _process(tmp_path)
    fund(bot.providers, user)
    guard = _guard(bot, user)
    with (
        pools.origin_scope(["new-origin"]),
        pytest.raises(spend_guard.SpendDenied, match="privacy"),
    ):
        _send(guard)
    guard.current.privacy = pools.Privacy(
        (PrivacyPolicy(name="work", allow=frozenset()),)
    )
    path.write_text(
        POOLS + "\nprivacy:\n  work:\n    bots: [devbox]\n    allow: [personal]\n"
    )
    with pytest.raises(spend_guard.SpendDenied, match="privacy"):
        _send(guard)


@pytest.mark.django_db
def test_orphaned_issued_history_cannot_mint_fresh_config_funds():
    import datetime as dt
    from decimal import Decimal

    from django.utils import timezone

    user = User.objects.create(username="lee")
    friend = User.objects.create(username="friend")
    orphan = SpendGrant.objects.create(
        pool="old-pool",
        owner=user,
        grantee=friend,
        routes=["old-provider/model"],
        budget_usd=Decimal(5),
        max_output_tokens=100,
        valid_from=timezone.now(),
        expires_at=timezone.now() + dt.timedelta(days=1),
    )
    assert orphan.envelope.parent_id is None
    from django_ergo.bots.providers import parse_providers

    providers = parse_providers(POOLS)
    assert pools.snapshot(providers, user.pk).get("paid").grant is None
    assert not SpendEnvelope.objects.filter(source="config").exists()


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    ("ref", "method"),
    [
        ("openai/gpt-6.1-sol", "_create_completion"),
        ("anthropic/claude-opus-5-5", "_create_message"),
    ],
)
def test_real_api_engine_refuses_stale_transport_before_fake_send(
    tmp_path, monkeypatch, ref, method
):
    from types import SimpleNamespace

    from asgiref.sync import async_to_sync

    user = User.objects.create(username="lee")
    path = tmp_path / "providers.yaml"
    path.write_text(POOLS)
    _, bot = _process(tmp_path)
    fund(bot.providers, user)
    session = chat(bot, user)
    pools.update_preference(user.pk, bot.providers, version=0, active_pool="paid")
    decision = pools.select_route(bot, session, ref=ref)
    engine = bot.make_engine(session, ref, decision=decision)
    sent = []

    async def send(*args, **kwargs):
        sent.append((args, kwargs))
        return SimpleNamespace(id="mock", usage=None)

    monkeypatch.setattr(
        engine,
        "_send_completion" if method == "_create_completion" else "_send_message",
        send,
    )
    env = (
        "TEST_POOL_OPENAI_KEY"
        if ref.startswith("openai/")
        else "TEST_POOL_ANTHROPIC_KEY"
    )
    path.write_text(POOLS.replace(env, "OTHER_ACCOUNT_SOURCE"))
    kwargs = {
        "model": decision.model,
        "messages": [{"role": "user", "content": "hello"}],
    }
    call = (
        (lambda: async_to_sync(engine._create_completion)(kwargs))
        if method == "_create_completion"
        else (lambda: async_to_sync(engine._create_message)(**kwargs))
    )
    with pytest.raises(spend_guard.SpendDenied, match="identity"):
        call()
    assert sent == []


@pytest.mark.django_db(transaction=True)
def test_migration_existing_history_starts_paused_without_consent():
    import datetime as dt

    from django.db import connection
    from django.db.migrations.executor import MigrationExecutor
    from django.utils import timezone

    before = ("django_ergo", "0061_spend_envelope_families")
    leaf = ("django_ergo", "0062_explicit_funding_and_paid_latch")
    try:
        MigrationExecutor(connection).migrate([before])
        executor = MigrationExecutor(connection)
        apps = executor.loader.project_state([before]).apps
        actor = apps.get_model("auth", "User").objects.create(username="old-owner")
        envelope = apps.get_model("django_ergo", "SpendEnvelope").objects.create(
            key=f"config:paid:{actor.pk}",
            pool="paid",
            source="config",
            owner_id=actor.pk,
            grantee_id=actor.pk,
        )
        grant = apps.get_model("django_ergo", "SpendGrant").objects.create(
            envelope=envelope,
            pool="paid",
            owner_id=actor.pk,
            grantee_id=actor.pk,
            routes=["openai/gpt-6.1-sol"],
            budget_usd="5",
            max_output_tokens=100,
            valid_from=timezone.now(),
            expires_at=timezone.now() + dt.timedelta(days=1),
            source="config",
        )
        apps.get_model("django_ergo", "InferenceRequest").objects.create(
            pool=spend_guard.ledger_key(grant.pk),
            provider="openai",
            model="gpt-6.1-sol",
            reserved_usd="5",
            cost_status="unknown",
        )
        executor.migrate([leaf])
        from django_ergo.conversation.models import PaidDispatchState
        from django_ergo.conversation.models import SpendFunding

        state = PaidDispatchState.objects.get(pk=1)
        assert state.paused and state.activated_at is not None
        assert not SpendFunding.objects.exists()
        assert spend_guard.spent_and_reserved(grant.pk)[1] == 5
        # This reverse is local maintenance only; real rollback requires an
        # independent paid-egress pause because it removes the latch schema.
        MigrationExecutor(connection).migrate([before])
        MigrationExecutor(connection).migrate([leaf])
        assert PaidDispatchState.objects.get(pk=1).paused
        assert spend_guard.spent_and_reserved(grant.pk)[1] == 5
    finally:
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())
