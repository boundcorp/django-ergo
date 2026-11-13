import json
from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from django.utils import timezone
from django_ergo.conversation.models import AgentUsage, ConversationSession, StructuredCall, Worker


@pytest.fixture
def lee(client):
    user = get_user_model().objects.create_superuser("lee", "lee@example.com", "pw")
    client.post(
        "/api/auth/login",
        json.dumps({"username": "lee", "password": "pw"}),
        content_type="application/json",
    )
    return user


@pytest.mark.django_db
def test_costs_by_kind_with_chat_replies_by_bot(client, lee):
    kitchen = ConversationSession.objects.create(user=lee, bot_name="kitchen", engine_type="openai")
    boundcorp = ConversationSession.objects.create(user=lee, bot_name="boundcorp", engine_type="claude")
    million = 1_000_000
    for session, model, tokens in [
        (kitchen, "gpt-6-luna", (2 * million, million)),  # $0.20 + $0.50
        (kitchen, "gpt-6-luna", (million, 0)),  # $0.10
        (boundcorp, "claude-sonnet-5-5", (million, million)),  # $2 + $10
    ]:
        StructuredCall.objects.create(
            kind="chat_reply",
            session=session,
            user=lee,
            model_name=model,
            input_tokens=tokens[0],
            output_tokens=tokens[1],
        )
    StructuredCall.objects.create(
        kind="compaction",
        user=lee,
        model_name="claude-haiku-4-5",
        output_tokens=million,
    )
    StructuredCall.objects.create(kind="title", user=lee, model_name="mystery", input_tokens=10)

    data = client.get("/api/costs?days=7").json()
    assert data["total"]["calls"] == 5
    assert data["total"]["cost"] == pytest.approx(0.8 + 12 + 5)
    assert data["total"]["unpriced_calls"] == 1
    assert data["unpriced_models"] == ["mystery"]
    kinds = {b["name"]: b["cost"] for b in data["by_kind"]}
    assert kinds == pytest.approx({"chat_reply": 12.8, "compaction": 5, "title": 0})
    assert [b["name"] for b in data["by_kind"]] == ["chat_reply", "compaction", "title"]
    bots = {b["name"]: (b["calls"], b["cost"]) for b in data["chat_reply_by_bot"]}
    assert bots == {
        "boundcorp": (1, pytest.approx(12)),
        "kitchen": (2, pytest.approx(0.8)),
    }
    assert len(data["by_day"]) == 7
    assert data["by_day"][-1]["calls"] == 5


@pytest.mark.django_db
def test_people_only_see_their_own_costs(client):
    other = get_user_model().objects.create_user("other", "o@example.com", "pw")
    StructuredCall.objects.create(kind="chat_reply", user=other, model_name="gpt-6-luna", input_tokens=5)
    get_user_model().objects.create_user("cook", "c@example.com", "pw")
    client.post(
        "/api/auth/login",
        json.dumps({"username": "cook", "password": "pw"}),
        content_type="application/json",
    )
    assert client.get("/api/costs").json()["total"]["calls"] == 0


@pytest.mark.django_db
def test_cache_writes_and_reads_are_billed_at_their_own_rates(client, lee):
    million = 1_000_000
    # claude-sonnet-5-5: input $2, cache write $2.50, cache read $0.20, output $10.
    StructuredCall.objects.create(
        kind="chat_reply",
        user=lee,
        model_name="claude-sonnet-5-5",
        input_tokens=million,
        cache_creation_input_tokens=million,
        cache_read_input_tokens=10 * million,
        output_tokens=million,
    )

    total = client.get("/api/costs?days=7").json()["total"]
    assert (
        total["input_tokens"],
        total["cache_write_tokens"],
        total["cache_read_tokens"],
    ) == (
        million,
        million,
        10 * million,
    )
    assert total["input_cost"] == pytest.approx(2)
    assert total["cache_write_cost"] == pytest.approx(2.5)
    assert total["cache_read_cost"] == pytest.approx(2)
    assert total["output_cost"] == pytest.approx(10)
    assert total["cost"] == pytest.approx(16.5)


@pytest.mark.django_db
def test_costs_use_the_cost_recorded_per_request_and_count_reasoning(client, lee):
    from decimal import Decimal

    StructuredCall.objects.create(
        kind="chat_reply",
        user=lee,
        model_name="gpt-6-sol",
        input_tokens=1_000_000,
        output_tokens=1_000_000,
        reasoning_tokens=300_000,
        cost_usd=Decimal("17"),  # recorded with a long-context surcharge
        metadata={
            "cost_parts": {
                "input": 4.0,
                "cache_write": 0.0,
                "cache_read": 0.0,
                "output": 13.0,
            }
        },
    )
    total = client.get("/api/costs").json()["total"]
    assert total["cost"] == pytest.approx(17)
    assert (total["input_cost"], total["output_cost"]) == (
        pytest.approx(4),
        pytest.approx(13),
    )
    assert total["reasoning_tokens"] == 300_000


@pytest.mark.django_db
def test_usage_groups_sessions_and_calculates_token_shares(client, lee):
    main = ConversationSession.objects.create(
        user=lee,
        bot_name="kitchen",
        engine_type="openai",
        transport_type="api",
        metadata={"bot_role": "root", "title": "Dinner"},
    )
    thread = ConversationSession.objects.create(
        user=lee,
        bot_name="kitchen",
        engine_type="openai",
        transport_type="api",
        metadata={"bot_role": "thread", "title": "Recipe"},
    )
    for session, kind, input_count, read_count, output_count in [
        (main, "chat_reply", 100, 300, 100),
        (main, "compaction", 0, 100, 0),
        (thread, "chat_reply", 100, 0, 100),
    ]:
        StructuredCall.objects.create(
            user=lee,
            session=session,
            kind=kind,
            model_name="gpt-6-luna",
            input_tokens=input_count,
            cache_read_input_tokens=read_count,
            output_tokens=output_count,
        )

    usage = client.get("/api/costs?days=7").json()["usage"]
    assert usage["headline"]["threads"] == 2
    assert usage["headline"]["tokens"] == 800
    assert usage["headline"]["cache_hit"] == pytest.approx(400 / 600)
    assert usage["headline"]["main_chats"] == pytest.approx(600 / 800)
    assert usage["headline"]["compaction"] == pytest.approx(100 / 800)
    assert [row["title"] for row in usage["threads"]] == ["Dinner", "Recipe"]
    assert usage["threads"][0]["cache_hit"] == pytest.approx(400 / 500)
    assert usage["threads"][0]["share"] == pytest.approx(600 / 800)


@pytest.mark.django_db
def test_subscription_tokens_count_without_api_spend(client, lee):
    cli = ConversationSession.objects.create(user=lee, bot_name="helper", engine_type="claude", transport_type="cli")
    api_session = ConversationSession.objects.create(
        user=lee, bot_name="helper", engine_type="claude", transport_type="api"
    )
    StructuredCall.objects.create(
        user=lee,
        session=cli,
        kind="chat_reply",
        model_name="mystery",
        input_tokens=400,
        output_tokens=100,
        cost_usd="99",
        metadata={"cost_parts": {"input": 99}},
    )
    StructuredCall.objects.create(
        user=lee,
        session=api_session,
        kind="chat_reply",
        model_name="claude-sonnet-5-5",
        input_tokens=1_000_000,
    )
    data = client.get("/api/costs").json()
    assert data["total"]["input_tokens"] == 1_000_400
    assert data["total"]["cost"] == pytest.approx(2)
    assert data["total"]["subscription_calls"] == 1
    assert data["total"]["unpriced_calls"] == 0
    assert data["unpriced_models"] == []
    assert data["usage"]["headline"]["subscription"] == pytest.approx(500 / 1_000_500)
    cli_row = next(row for row in data["usage"]["threads"] if row["subscription"])
    assert cli_row["tokens"] == 500
    assert cli_row["cost"] == 0
    assert cli_row["title"] == "helper main"


@pytest.mark.django_db
def test_usage_respects_user_scope_and_bot_filter(client):
    users = get_user_model().objects
    cook = users.create_user("cook", "c@example.com", "pw")
    other = users.create_user("other", "o@example.com", "pw")
    for user, bot in [(cook, "kitchen"), (cook, "helper"), (other, "kitchen")]:
        session = ConversationSession.objects.create(
            user=user, bot_name=bot, engine_type="openai", transport_type="api"
        )
        StructuredCall.objects.create(
            user=user,
            session=session,
            kind="chat_reply",
            model_name="gpt-6-luna",
            input_tokens=10,
        )
    client.post(
        "/api/auth/login",
        json.dumps({"username": "cook", "password": "pw"}),
        content_type="application/json",
    )
    usage = client.get("/api/costs?bot=kitchen").json()["usage"]
    assert usage["headline"]["threads"] == 1
    assert usage["headline"]["tokens"] == 10
    assert [row["bot"] for row in usage["threads"]] == ["kitchen"]


@pytest.mark.django_db
def test_agent_usage_has_its_own_scoped_and_filtered_costs_block(client):
    cook = get_user_model().objects.create_user("cook", "cook@example.com", "pw")
    other = get_user_model().objects.create_user("other", "other@example.com", "pw")
    kitchen = ConversationSession.objects.create(user=cook, bot_name="kitchen", engine_type="openai")
    helper = ConversationSession.objects.create(user=cook, bot_name="helper", engine_type="openai")
    hidden = ConversationSession.objects.create(user=other, bot_name="kitchen", engine_type="openai")
    rows = [
        (kitchen, "kitchen", "Fix recipe", "claude-sonnet", 10, 20),
        (helper, "helper", "Fix helper", "gpt-6-codex", 5, 5),
        (hidden, "kitchen", "Private work", "claude-opus", 99, 99),
    ]
    for session, bot, title, model, input_tokens, output_tokens in rows:
        worker = Worker.objects.create(
            session=session, bot_name=bot, title=title, function="orca:watch", status="completed"
        )
        AgentUsage.objects.create(
            worker=worker,
            session=session,
            bot_name=bot,
            agent="codex",
            model=model,
            input_tokens=input_tokens,
            cache_read_tokens=5,
            output_tokens=output_tokens,
            requests=2,
            last_at=timezone.now(),
        )
    client.post(
        "/api/auth/login",
        json.dumps({"username": "cook", "password": "pw"}),
        content_type="application/json",
    )

    agents = client.get("/api/costs?bot=kitchen").json()["agents"]

    assert agents["headline"]["sessions"] == 1
    assert agents["headline"]["tokens"] == 35
    assert agents["headline"]["cache_hit"] == pytest.approx(5 / 15)
    assert agents["headline"]["unpriced_rows"] == 1
    assert agents["headline"]["billed_usd"] is None
    rows = [{k: v for k, v in r.items() if k not in ("first_at", "last_at", "spillover")} for r in agents["rows"]]
    assert rows == [
        {
            "worker_id": str(Worker.objects.get(title="Fix recipe").id),
            "worker_title": "Fix recipe",
            "agent": "codex",
            "model": "claude-sonnet",
            "chat_id": str(kitchen.id),
            "chat_title": "kitchen main",
            "bot": "kitchen",
            "worker_status": "completed",
            "input_tokens": 10,
            "cache_write_tokens": 0,
            "cache_read_tokens": 5,
            "output_tokens": 20,
            "reasoning_tokens": 0,
            "tokens": 35,
            "cache_hit": pytest.approx(5 / 15),
            "requests": 2,
            "project_key": "",
            "project": "",
            "project_remote": "",
            "branch": "",
            "cost": 0.0,
            "priced": False,
        }
    ]

    AgentUsage.objects.filter(session=kitchen).update(last_at=timezone.now() - timezone.timedelta(days=31))
    assert client.get("/api/costs?bot=kitchen").json()["agents"]["rows"] == []


@pytest.mark.django_db
def test_agent_usage_by_project_with_list_price_estimates(client, lee):
    session = ConversationSession.objects.create(user=lee, bot_name="devbox", engine_type="claude")
    million = 1_000_000
    holyword = {"key": "remote:aaa", "name": "holyword", "remote": "ssh://github.com/boundcorp/holyword"}
    meditate = {"key": "remote:bbb", "name": "meditate", "remote": "ssh://github.com/boundcorp/meditate"}
    rows = [
        ("Fix verses", holyword, "claude", "claude-opus-5-5", million, million),  # $4 + $20
        ("Add search", holyword, "codex", "gpt-6-sol", million, 0),  # $2
        ("Timer", meditate, "omp", "gpt-6-sol", 0, million),  # $10
        ("Old work", None, "codex", "mystery-model", 10, 10),
    ]
    for title, project, agent, model, input_tokens, output_tokens in rows:
        worker = Worker.objects.create(
            session=session,
            bot_name="devbox",
            title=title,
            function="agent:orca",
            status="completed",
            state={"agent": agent, **({"project": {**project, "branch": "main"}} if project else {})},
        )
        AgentUsage.objects.create(
            worker=worker,
            session=session,
            bot_name="devbox",
            agent=agent,
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            requests=3,
            last_at=timezone.now(),
        )

    agents = client.get("/api/costs").json()["agents"]

    assert agents["headline"]["projects"] == 2
    assert agents["headline"]["cost"] == pytest.approx(36)
    assert agents["headline"]["priced_rows"] == 3
    assert agents["headline"]["unpriced_rows"] == 1
    assert agents["headline"]["billed_usd"] is None
    by_project = {p["name"]: p for p in agents["by_project"]}
    assert [p["name"] for p in agents["by_project"]] == ["holyword", "meditate", "(no project recorded)"]
    assert by_project["holyword"]["workers"] == 2
    assert by_project["holyword"]["agents"] == ["claude", "codex"]
    assert by_project["holyword"]["cost"] == pytest.approx(26)
    assert by_project["holyword"]["remote"] == "ssh://github.com/boundcorp/holyword"
    assert by_project["meditate"]["tokens"] == million
    assert by_project["(no project recorded)"]["unpriced_models"] == ["mystery-model"]
    timer = next(row for row in agents["rows"] if row["worker_title"] == "Timer")
    assert (timer["project"], timer["branch"], timer["priced"]) == ("meditate", "main", True)


@pytest.mark.django_db
def test_private_pool_spend_by_pool_provider_and_model(client, lee):
    from decimal import Decimal

    from django_ergo.conversation.models import InferenceRequest

    kimi = "accounts/fireworks/models/kimi-k3"
    common = {"user": lee, "bot_name": "personal", "selector": "personal/heavy", "pool": "personal"}
    InferenceRequest.objects.create(
        **common,
        provider="fireworks",
        model=kimi,
        outcome="ok",
        cost_status="priced",
        cost_usd=Decimal("0.00492"),
        prompt_tokens=1000,
        cached_tokens=400,
        output_tokens=200,
        reasoning_tokens=50,
    )
    # Sent but unpriced (or no usage): unknown, never counted as $0.
    InferenceRequest.objects.create(
        **common, provider="fireworks", model=kimi, outcome="error", cost_status="unknown", reserved_usd=Decimal("0.2")
    )
    # Refused before HTTP: recorded, not sent.
    InferenceRequest.objects.create(
        **common, provider="fireworks", model=kimi, outcome="not_sent", cost_status="not_sent", cost_usd=0
    )

    private = client.get("/api/costs").json()["private"]

    assert private["cost"] == pytest.approx(0.00492)
    assert private["unknown_cost_requests"] == 1
    [row] = private["rows"]
    assert (row["pool"], row["provider"], row["model"], row["bot"], row["billing_mode"]) == (
        "personal",
        "fireworks",
        kimi,
        "personal",
        "api",
    )
    assert (row["requests"], row["sent"], row["prompt_tokens"], row["cached_tokens"]) == (3, 2, 1000, 400)
    assert row["unsettled_reserved"] == pytest.approx(0.2)


@pytest.mark.django_db
def test_session_usage_by_hour_splits_the_chat_from_its_agents(client, lee):
    from datetime import timedelta

    from django_ergo.conversation.models import AgentSession, AgentSessionThreadLink, AgentUsageEvent

    chat = ConversationSession.objects.create(user=lee, bot_name="devbox", engine_type="claude")
    other = ConversationSession.objects.create(user=lee, bot_name="devbox", engine_type="claude")
    now = timezone.now()

    def call(session, ago, tokens):
        made = StructuredCall.objects.create(
            kind="chat_reply", session=session, user=lee, input_tokens=tokens, cache_read_input_tokens=tokens
        )
        StructuredCall.objects.filter(pk=made.pk).update(created_at=now - ago)

    call(chat, timedelta(minutes=10), 100)  # newest hour: 200
    call(chat, timedelta(hours=2, minutes=30), 50)  # third from the end: 100
    call(chat, timedelta(hours=7), 1000)  # before the window
    call(other, timedelta(minutes=5), 999)  # another chat

    # A worker whose transcript was captured: counted per event, not by its running totals.
    captured = Worker.objects.create(session=chat, bot_name="devbox", title="fix", function="orca:watch")
    native = AgentSession.objects.create(cli="omp", host_namespace="local", native_session_id="n1")
    AgentSessionThreadLink.objects.create(session=native, conversation=chat, worker=captured)
    AgentUsageEvent.objects.create(
        session=native, source_event_key="a", occurred_at=now - timedelta(minutes=20), input_tokens=30, output_tokens=10
    )
    AgentUsage.objects.create(
        worker=captured,
        session=chat,
        bot_name="devbox",
        agent="omp",
        model="m",
        input_tokens=10_000,
        first_at=now - timedelta(hours=1),
        last_at=now,
    )
    # One without: its 600 tokens over the last 3 hours spread 200 an hour.
    plain = Worker.objects.create(session=chat, bot_name="devbox", title="docs", function="orca:watch")
    AgentUsage.objects.create(
        worker=plain,
        session=chat,
        bot_name="devbox",
        agent="claude",
        model="m",
        output_tokens=600,
        first_at=now - timedelta(hours=3),
        last_at=now,
    )

    data = client.get(f"/api/sessions/{chat.id}/usage?hours=6").json()
    assert [h["thread_tokens"] for h in data["hours"]] == [0, 0, 0, 100, 0, 200]
    agents = [h["agent_tokens"] for h in data["hours"]]
    assert agents[:3] == [0, 0, 0]
    assert sum(agents[3:]) == pytest.approx(640, abs=2)
    assert agents[5] == pytest.approx(240, abs=2)
    assert data["thread_tokens"] == 300
    assert data["agent_tokens"] == sum(agents)


@pytest.mark.django_db
def test_session_usage_is_only_for_visible_chats(client):
    owner = get_user_model().objects.create_user("other", "o@example.com", "pw")
    chat = ConversationSession.objects.create(user=owner, bot_name="devbox")
    get_user_model().objects.create_user("cook", "c@example.com", "pw")
    client.post(
        "/api/auth/login",
        json.dumps({"username": "cook", "password": "pw"}),
        content_type="application/json",
    )
    assert client.get(f"/api/sessions/{chat.id}/usage").status_code == 404


@pytest.mark.django_db
def test_calls_that_ran_for_a_chat_count_for_it_and_its_bot(client, lee):
    # A browser task's driver call has no session of its own; its metadata
    # names the chat it ran for.
    chat = ConversationSession.objects.create(user=lee, bot_name="personal", user_title="Vault")
    other = ConversationSession.objects.create(user=lee, bot_name="devbox")
    StructuredCall.objects.create(kind="chat_reply", session=chat, user=lee, model_name="m", input_tokens=100)
    StructuredCall.objects.create(
        kind="browser_task",
        user=lee,
        model_name="m",
        input_tokens=900,
        metadata={"session": str(chat.id), "worker": "w"},
    )
    StructuredCall.objects.create(kind="chat_reply", session=other, user=lee, model_name="m", input_tokens=50)
    StructuredCall.objects.create(kind="compaction", user=lee, model_name="m", input_tokens=5)
    StructuredCall.objects.create(kind="title", user=lee, model_name="m", metadata={"session": "not-a-uuid"})

    personal = client.get("/api/costs?days=7&bot=personal").json()
    assert {b["name"]: b["calls"] for b in personal["by_kind"]} == {"chat_reply": 1, "browser_task": 1}
    (thread,) = personal["usage"]["threads"]
    assert (thread["title"], thread["tokens"]) == ("Vault", 1000)

    everyone = client.get("/api/costs?days=7").json()
    assert everyone["total"]["calls"] == 5  # counted once
    assert {row["title"]: row["tokens"] for row in everyone["usage"]["threads"]}["Vault"] == 1000

    usage = client.get(f"/api/sessions/{chat.id}/usage?hours=1").json()
    assert usage["thread_tokens"] == 1000


@pytest.mark.django_db
def test_estimate_prices_subscription_bot_calls_and_agents_at_list_and_reports_coverage(client, lee):
    million = 1_000_000
    cli = ConversationSession.objects.create(user=lee, bot_name="helper", engine_type="claude", transport_type="cli")
    api = ConversationSession.objects.create(user=lee, bot_name="helper", engine_type="claude", transport_type="api")
    # claude-sonnet-5-5: input $2, cache write $2.50, cache read $0.20, output $10.
    for session in (cli, api):
        StructuredCall.objects.create(
            user=lee,
            session=session,
            kind="chat_reply",
            model_name="claude-sonnet-5-5",
            input_tokens=million,
            cache_creation_input_tokens=million,
            cache_read_input_tokens=2 * million,
            output_tokens=million,
        )
    StructuredCall.objects.create(user=lee, session=cli, kind="chat_reply", model_name="mystery", input_tokens=10)
    StructuredCall.objects.create(user=lee, session=api, kind="title", model_name="claude-sonnet-5-5")
    now = timezone.now()
    for model, last in (("gpt-6-sol", now), ("mystery-agent", now)):
        AgentUsage.objects.create(
            session=api,
            bot_name="helper",
            agent="codex",
            model=model,
            input_tokens=million,
            requests=1,
            first_at=now - timedelta(days=30),  # began before the window: lifetime totals
            last_at=last,
        )

    data = client.get("/api/costs?days=7").json()
    # The actual-cost helper still bills the subscription call at $0.
    assert data["total"]["cost"] == pytest.approx(14.9)
    bot = data["estimate"]["bot"]
    assert bot["list_cost"] == pytest.approx(29.8)  # the CLI call counts at list price
    assert bot["output_cost"] == pytest.approx(20)
    assert bot["cache_write_cost"] == pytest.approx(5)
    assert (bot["unpriced_rows"], bot["priced_rows"], bot["no_usage_rows"]) == (1, 2, 1)
    assert bot["unpriced_tokens"] == 10
    assert bot["unpriced_models"] == ["mystery"]
    assert bot["exact_window"] is True
    # Weighted over all rows, not an average of per-row ratios: output excluded.
    denominator = 2 * (million + million + 2 * million) + 10
    assert bot["cache_denominator"] == denominator
    assert bot["cache_hit"] == pytest.approx(4 * million / denominator)
    agents = data["estimate"]["agents"]
    assert agents["list_cost"] == pytest.approx(2)
    assert agents["unpriced_models"] == ["mystery-agent"]
    assert (agents["spillover_rows"], agents["spillover_tokens"], agents["exact_window"]) == (2, 2 * million, False)
    combined = data["estimate"]["combined"]
    assert combined["list_cost"] == pytest.approx(31.8)
    assert combined["unpriced_rows"] == 2
    assert combined["priced_tokens"] + combined["unpriced_tokens"] == combined["tokens"]
    assert all(row["spillover"] for row in data["agents"]["rows"])


@pytest.mark.django_db
def test_estimate_without_cache_denominator_has_no_hit_rate(client, lee):
    StructuredCall.objects.create(user=lee, kind="chat_reply", model_name="gpt-6-luna", output_tokens=5)
    estimate = client.get("/api/costs").json()["estimate"]
    assert estimate["bot"]["cache_hit"] is None
    assert estimate["agents"]["cache_hit"] is None
    assert estimate["agents"]["rows"] == 0


@pytest.mark.django_db
def test_window_is_explicit_utc_days_and_series_reconciles_with_totals(client, lee):
    now = timezone.now()
    since = (now - timedelta(days=2)).replace(hour=0, minute=0, second=0, microsecond=0)
    inside = StructuredCall.objects.create(user=lee, kind="title", model_name="gpt-6-luna", input_tokens=1)
    before = StructuredCall.objects.create(user=lee, kind="title", model_name="gpt-6-luna", input_tokens=1)
    StructuredCall.objects.filter(pk=inside.pk).update(created_at=since)  # first instant of the first day
    StructuredCall.objects.filter(pk=before.pk).update(created_at=since - timedelta(microseconds=1))
    data = client.get("/api/costs?days=3").json()
    assert data["timezone"] == "UTC"
    assert data["since"] == since.isoformat()
    assert [d["date"] for d in data["by_day"]] == [
        since.date().isoformat(),
        (since + timedelta(days=1)).date().isoformat(),
        now.date().isoformat(),
    ]
    assert data["by_day"][0]["calls"] == 1
    assert sum(d["calls"] for d in data["by_day"]) == data["total"]["calls"] == 1


@pytest.mark.django_db
def test_agent_rows_outside_the_window_are_dropped_and_spillover_is_flagged(client, lee):
    session = ConversationSession.objects.create(user=lee, bot_name="devbox", engine_type="claude")
    now = timezone.now()
    since = (now - timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    for model, first, last in (
        ("gpt-6-sol", now - timedelta(hours=1), now),
        ("gpt-6-luna", since - timedelta(days=5), now),
        ("gpt-6-terra", since - timedelta(days=5), since - timedelta(days=4)),
    ):
        AgentUsage.objects.create(
            session=session,
            bot_name="devbox",
            agent="codex",
            model=model,
            input_tokens=10,
            first_at=first,
            last_at=last,
        )
    rows = {r["model"]: r for r in client.get("/api/costs?days=2").json()["agents"]["rows"]}
    assert set(rows) == {"gpt-6-sol", "gpt-6-luna"}
    assert rows["gpt-6-sol"]["spillover"] is False
    assert rows["gpt-6-luna"]["spillover"] is True


@pytest.mark.django_db
def test_estimate_uses_table_not_recorded_cost_and_missing_tokens_are_unknown(client, lee):
    StructuredCall.objects.create(
        user=lee,
        kind="title",
        model_name="gpt-6-sol",
        input_tokens=1_000_000,
        cost_usd=99,
        metadata={"cost_parts": {"input": 99}},
    )
    StructuredCall.objects.create(user=lee, kind="title", model_name="gpt-6-sol")
    session = ConversationSession.objects.create(user=lee, bot_name="helper", engine_type="claude")
    AgentUsage.objects.create(
        session=session,
        bot_name="helper",
        agent="codex",
        model="gpt-6-sol",
        requests=1,
        last_at=timezone.now(),
    )
    response = client.get("/api/costs")
    assert response.status_code == 200
    data = response.json()
    assert data["total"]["cost"] == pytest.approx(99)
    bot = data["estimate"]["bot"]
    assert bot["list_cost"] == pytest.approx(2)
    assert (bot["priced_rows"], bot["unpriced_rows"], bot["no_usage_rows"]) == (1, 0, 1)
    assert "per-request surcharges" in data["estimate"]["basis"]
    assert data["estimate"]["agents"]["no_usage_rows"] == 1
    assert data["agents"]["headline"]["priced_rows"] == 0
    assert data["agents"]["rows"][0]["priced"] is False


@pytest.mark.django_db
def test_costs_window_normalizes_non_utc_now_and_serializes_schema(client, lee, settings):
    from datetime import UTC, datetime
    from datetime import timezone as dt_timezone
    from unittest.mock import patch

    settings.TIME_ZONE = "America/Los_Angeles"
    now = datetime(2026, 11, 13, 18, 30, tzinfo=dt_timezone(timedelta(hours=-8)))
    with patch("ergonaut.api.costs.timezone.now", return_value=now):
        response = client.get("/api/costs?days=2")
    assert response.status_code == 200
    data = response.json()
    assert data["since"] == datetime(2026, 11, 13, tzinfo=UTC).isoformat()
    assert data["until"] == now.astimezone(UTC).isoformat()
    assert [day["date"] for day in data["by_day"]] == ["2026-11-13", "2026-11-14"]
    assert data["estimate"]["bot"]["rows"] == 0


def _ledger_row(owner, key, kind, amount, *, bot="kitchen", currency="USD", at=None, **extra):
    from decimal import Decimal

    from django_ergo.costs.ledger import record_observation
    from django_ergo.costs.types import CostKind, ObservationSpec, OperationSpec

    return record_observation(
        actor=owner,
        operation=OperationSpec("p", "acct", key, currency, bot_name=bot),
        observation=ObservationSpec(
            "s",
            f"{key}-{kind}",
            CostKind(kind),
            extra.pop("revision", 1),
            amount=None if amount is None else Decimal(amount),
            occurred_at=at or timezone.now(),
            **extra,
        ),
    )


@pytest.mark.django_db
def test_ledger_projection_is_owner_scoped_bot_filtered_and_keeps_unknown_null(client):
    User = get_user_model()
    ann = User.objects.create_user("ann", password="pw")
    bob = User.objects.create_user("bob", password="pw")
    _ledger_row(ann, "a1", "estimate", "1.5")
    _ledger_row(ann, "a2", "unknown", None, currency="CREDITS", bot="ads")
    _ledger_row(ann, "a3", "reservation", "9", bot="ads")
    _ledger_row(bob, "b1", "estimate", "100")
    client.post("/api/auth/login", json.dumps({"username": "ann", "password": "pw"}), content_type="application/json")
    data = client.get("/api/costs/ledger?days=7").json()
    rows = {(b["kind"], b["currency"]): b for b in data["buckets"]}
    assert rows[("estimate", "USD")]["known_amount"] == "1.5"  # Bob's 100 is invisible
    assert rows[("unknown", "CREDITS")]["known_amount"] is None
    assert rows[("unknown", "CREDITS")]["unknown_count"] == 1
    assert rows[("reservation", "USD")]["known_amount"] == "9"
    assert data["coverage"]["live_ingestion"] is False and data["coverage"]["complete"] is False
    only_ads = client.get("/api/costs/ledger?bot=ads").json()
    assert {(b["kind"], b["currency"]) for b in only_ads["buckets"]} == {
        ("unknown", "CREDITS"),
        ("reservation", "USD"),
    }


@pytest.mark.django_db
def test_ledger_projection_requires_authentication_and_superuser_sees_all(client, lee):
    assert Client().get("/api/costs/ledger").status_code in (401, 403)
    other = get_user_model().objects.create_user("zed", password="pw")
    _ledger_row(other, "z1", "estimate", "2")
    _ledger_row(lee, "l1", "estimate", "3")
    (bucket,) = client.get("/api/costs/ledger").json()["buckets"]
    assert bucket["known_amount"] == "5"


@pytest.mark.django_db
def test_ledger_window_excludes_old_rows_after_supersession(client, lee):
    old = timezone.now() - timedelta(days=60)
    _ledger_row(lee, "w1", "estimate", "1")
    _ledger_row(lee, "w1", "final", "5", at=old, revision=2)
    assert client.get("/api/costs/ledger?days=7").json()["buckets"] == []
