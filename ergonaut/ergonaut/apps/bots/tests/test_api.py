import json
import textwrap

import pytest
from django.contrib.auth import get_user_model

from django_ergo.bots import webhooks
from ergonaut.apps.bots.loading import load_registry
from ergonaut.apps.bots.tests.fakes import fake_registry
from ergonaut.apps.bots.tests.fakes import say
from ergonaut.apps.bots.tests.fakes import tool_call

BOT = """
name: kitchen
description: Runs the kitchen
engine: {type: claude}
orchestration: false
tools: [tools/pantry.py]
"""

TOOLS = """
from django_ergo.bots import bot_tool

@bot_tool
def pantry_count(item: str) -> int:
    return 4

@bot_tool(requires_approval=True)
def order(item: str) -> str:
    return f"ordered {item}"
"""


@pytest.fixture
def bot_folder(tmp_path):
    folder = tmp_path / "kitchen"
    (folder / "tools").mkdir(parents=True)
    (folder / "agents.md").write_text("You run the kitchen.")
    (folder / "tools" / "pantry.py").write_text(textwrap.dedent(TOOLS))
    (folder / "bot.yaml").write_text(textwrap.dedent(BOT))
    return folder


@pytest.fixture
def use_bots(bot_folder):
    def install(*responses):
        registry, client = fake_registry(bot_folder, *responses)
        webhooks.set_registry(registry)
        return client

    yield install
    webhooks.set_registry(load_registry)


@pytest.fixture
def cook(client):
    user = get_user_model().objects.create_user("cook", "cook@example.com", "pw")
    response = client.post(
        "/api/auth/login", json.dumps({"username": "cook", "password": "pw"}), content_type="application/json"
    )
    assert response.status_code == 200
    return user


def post(client, url, data=None):
    return client.post(url, json.dumps(data or {}), content_type="application/json")


@pytest.mark.django_db(transaction=True)
def test_login_required(client):
    assert client.get("/api/bots").status_code == 401
    bad = post(client, "/api/auth/login", {"username": "x", "password": "y"})
    assert bad.status_code == 401


@pytest.mark.django_db(transaction=True)
def test_chat_with_the_root_session_and_drill_into_tools(client, cook, use_bots):
    fake = use_bots(tool_call("pantry_count", {"item": "eggs"}), say("You have 4 eggs.", ["Add eggs to the list"]))

    bots = client.get("/api/bots").json()
    assert bots == [
        {"name": "kitchen", "description": "Runs the kitchen", "orchestration": False, "root_session_id": None}
    ]
    root = post(client, "/api/bots/kitchen/root").json()
    assert root["role"] == "root"
    assert root["title"] == "Root chat"

    turn = post(client, f"/api/sessions/{root['id']}/messages", {"text": "How many eggs?"}).json()
    assert turn["text"] == "You have 4 eggs."
    assert turn["suggestions"] == ["Add eggs to the list"]
    assert turn["type"] == "message"
    assert len(fake.calls) == 2

    detail = client.get(f"/api/sessions/{root['id']}").json()
    blocks = [b for m in detail["messages"] for b in m["blocks"]]
    tool_use = next(b for b in blocks if b["type"] == "tool_use" and b["name"] == "pantry_count")
    assert tool_use["input"] == {"item": "eggs"}
    result = next(b for b in blocks if b["type"] == "tool_result" and b.get("name") == "pantry_count")
    assert result["content"] == "4"
    [call] = detail["calls"]
    assert call["kind"] == "chat_reply"
    assert call["status"] == "completed"
    assert call["response"]["text"] == "You have 4 eggs."

    call_detail = client.get(f"/api/calls/{call['id']}").json()
    assert call_detail["id"] == call["id"]
    assert call_detail["output_tokens"] > 0

    assert client.get("/api/bots").json()[0]["root_session_id"] == root["id"]
    listed = client.get("/api/sessions?q=eggs").json()
    assert [s["id"] for s in listed] == [root["id"]]
    assert client.get("/api/sessions?q=zucchini").json() == []


@pytest.mark.django_db(transaction=True)
def test_threads_and_approvals(client, cook, use_bots):
    use_bots(tool_call("order", {"item": "milk"}), say("Ordered milk."))
    thread = post(client, "/api/bots/kitchen/threads", {"title": "Groceries"}).json()
    assert thread["title"] == "Groceries"
    assert thread["parent_id"] is not None

    turn = post(client, f"/api/sessions/{thread['id']}/messages", {"text": "Order milk"}).json()
    assert turn["approvals"] == [{"id": turn["approvals"][0]["id"], "name": "order", "input": {"item": "milk"}}]

    done = post(client, f"/api/sessions/{thread['id']}/approvals", {"approve": True}).json()
    assert done["text"] == "Ordered milk."
    again = post(client, f"/api/sessions/{thread['id']}/approvals", {"approve": True})
    assert again.status_code == 409

    closed = post(client, f"/api/sessions/{thread['id']}/close").json()
    assert closed["status"] == "completed"


@pytest.mark.django_db(transaction=True)
def test_people_only_see_their_own_sessions(client, cook, use_bots):
    use_bots(say("hi"))
    other = get_user_model().objects.create_user("other", "o@example.com", "pw")
    from django_ergo.conversation.models import ConversationSession

    theirs = ConversationSession.objects.create(user=other, bot_name="kitchen", engine_type="claude")
    assert client.get(f"/api/sessions/{theirs.id}").status_code == 404
    assert client.get("/api/sessions").json() == []
    assert post(client, "/api/bots/nobody/root").status_code == 404
