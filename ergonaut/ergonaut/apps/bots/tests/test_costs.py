import json

import pytest
from django.contrib.auth import get_user_model
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

    assert agents["headline"] == {
        "sessions": 1,
        "tokens": 35,
        "cache_hit": pytest.approx(5 / 15),
        "projects": 0,
        "cost": 0.0,
    }
    assert agents["rows"] == [
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
