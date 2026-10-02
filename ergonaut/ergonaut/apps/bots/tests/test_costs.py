import json

import pytest
from django.contrib.auth import get_user_model

from django_ergo.conversation.models import ConversationSession
from django_ergo.conversation.models import StructuredCall


@pytest.fixture
def lee(client):
    user = get_user_model().objects.create_superuser("lee", "lee@example.com", "pw")
    client.post("/api/auth/login", json.dumps({"username": "lee", "password": "pw"}), content_type="application/json")
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
            kind="chat_reply", session=session, user=lee, model_name=model,
            input_tokens=tokens[0], output_tokens=tokens[1],
        )
    StructuredCall.objects.create(kind="compaction", user=lee, model_name="claude-haiku-4-5", output_tokens=million)
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
    assert bots == {"boundcorp": (1, pytest.approx(12)), "kitchen": (2, pytest.approx(0.8))}
    assert len(data["by_day"]) == 7
    assert data["by_day"][-1]["calls"] == 5


@pytest.mark.django_db
def test_people_only_see_their_own_costs(client):
    other = get_user_model().objects.create_user("other", "o@example.com", "pw")
    StructuredCall.objects.create(kind="chat_reply", user=other, model_name="gpt-6-luna", input_tokens=5)
    get_user_model().objects.create_user("cook", "c@example.com", "pw")
    client.post("/api/auth/login", json.dumps({"username": "cook", "password": "pw"}), content_type="application/json")
    assert client.get("/api/costs").json()["total"]["calls"] == 0
