import json

import pytest
from django.contrib.auth import get_user_model
from django_ergo.conversation.models import ConversationSession, StructuredCall


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
