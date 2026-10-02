import json
import textwrap

import pytest
from django.contrib.auth import get_user_model
from django_ergo.bots import webhooks

from ergonaut.apps.bots.loading import load_registry
from ergonaut.apps.bots.tests.fakes import fake_registry, say, tool_call

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
        {
            "name": "kitchen",
            "description": "Runs the kitchen",
            "orchestration": False,
            "knowledge": False,
            "parent": "",
            "root_session_id": None,
        }
    ]
    root = post(client, "/api/bots/kitchen/root").json()
    assert root["role"] == "root"
    assert root["title"] == "Chat"

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
def test_bot_page_lists_tools_and_skills_and_threads_follow_orchestration(client, cook, bot_folder, use_bots):
    (bot_folder / "skills").mkdir()
    (bot_folder / "skills" / "meal-planning.md").write_text(
        "---\ndescription: Plan a week of dinners\n---\nLook back 60 days."
    )
    use_bots(say("hi"))
    bot = client.get("/api/bots/kitchen").json()
    assert bot["orchestration"] is False
    assert bot["engine"] == "claude"
    assert bot["instructions"] == "You run the kitchen."
    tools = {t["name"]: t for t in bot["tools"]}
    assert {"pantry_count", "order", "list_skills", "load_skill"} <= set(tools)
    assert "send_reply" not in tools  # the reply format, not a tool
    assert not [name for name in tools if name.startswith("threads_")]
    assert tools["order"]["requires_approval"] is True
    assert tools["pantry_count"]["requires_approval"] is False
    assert bot["skills"] == [
        {"name": "meal-planning", "description": "Plan a week of dinners", "body": "Look back 60 days."}
    ]

    refused = post(client, "/api/bots/kitchen/threads", {"title": "Nope"})
    assert refused.status_code == 409

    root = post(client, "/api/bots/kitchen/root").json()
    post(client, f"/api/sessions/{root['id']}/messages", {"text": "hi"})
    [call] = client.get(f"/api/sessions/{root['id']}").json()["calls"]
    assert {"pantry_count", "load_skill"} <= set(call["tools"])
    assert "send_reply" not in call["tools"]


@pytest.mark.django_db(transaction=True)
def test_threads_and_approvals(client, cook, bot_folder, use_bots):
    (bot_folder / "bot.yaml").write_text(textwrap.dedent(BOT).replace("orchestration: false", "orchestration: true"))
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


@pytest.mark.django_db(transaction=True)
def test_browser_writes_pass_the_csrf_check(use_bots):
    """The web app's flow: fetch the CSRF cookie, log in, then write with the header."""
    from django.test import Client

    use_bots(say("hi"))
    get_user_model().objects.create_user("cook", "cook@example.com", "pw")
    browser = Client(enforce_csrf_checks=True)
    browser.get("/api/auth/csrf")
    token = browser.cookies["csrftoken"].value

    def write(url, data=None):
        return browser.post(url, json.dumps(data or {}), content_type="application/json", headers={"X-CSRFToken": token})

    assert write("/api/auth/login", {"username": "cook", "password": "pw"}).status_code == 200
    token = browser.cookies["csrftoken"].value  # rotated on login
    root = write("/api/bots/kitchen/root")
    assert root.status_code == 200, root.content
    turn = write(f"/api/sessions/{root.json()['id']}/messages", {"text": "hello"})
    assert turn.status_code == 200, turn.content
    assert browser.post("/api/bots/kitchen/root").status_code == 403


@pytest.mark.django_db(transaction=True)
def test_browse_a_bots_knowledge_base(client, cook, bot_folder, use_bots):
    kb = bot_folder / "kb"
    (kb / "recipes").mkdir(parents=True)
    (kb / "index.md").write_text("# Kitchen\n\nChocolate Soylent for breakfast most days.")
    (kb / "recipes" / "tacos.md").write_text("# Tacos\n\nTuesdays.")
    use_bots(say("hi"))
    [found] = client.get("/api/bots/kitchen/kbs").json()
    assert found["kind"] == "folder"
    assert found["articles"] == [
        {"path": "index.md", "title": "Kitchen", "root": True},
        {"path": "recipes/tacos.md", "title": "Tacos", "root": False},
    ]
    article = client.get(f"/api/bots/kitchen/kbs/{found['id']}/article?path=recipes/tacos.md").json()
    assert article == {"path": "recipes/tacos.md", "title": "Tacos", "body": "# Tacos\n\nTuesdays."}
    assert client.get(f"/api/bots/kitchen/kbs/{found['id']}/article?path=../bot.yaml").status_code == 404
    assert client.get("/api/bots/kitchen/kbs/nope/article?path=index.md").status_code == 404


@pytest.mark.django_db(transaction=True)
def test_upload_list_download_and_delete_session_files(client, cook, use_bots, settings, tmp_path):
    from django.core.files.uploadedfile import SimpleUploadedFile
    from django_ergo.conversation.models import ConversationSession

    settings.MEDIA_ROOT = str(tmp_path / "media")
    use_bots(say("hi"))
    root = post(client, "/api/bots/kitchen/root").json()
    upload = client.post(
        f"/api/sessions/{root['id']}/attachments",
        {"file": SimpleUploadedFile("pantry.csv", b"item,count\neggs,4\n", content_type="text/csv")},
    )
    assert upload.status_code == 200, upload.content
    file = upload.json()
    assert (file["filename"], file["source"], file["media_type"], file["size"]) == (
        "pantry.csv", "upload", "text/csv", 18,
    )
    listed = client.get(f"/api/sessions/{root['id']}/attachments").json()
    assert [f["id"] for f in listed] == [file["id"]]
    download = client.get(f"/api/attachments/{file['id']}/download")
    assert b"".join(download.streaming_content) == b"item,count\neggs,4\n"

    # Someone else can't see it.
    other = get_user_model().objects.create_user("other", "o@example.com", "pw")
    client.force_login(other)
    assert client.get(f"/api/attachments/{file['id']}/download").status_code == 404
    assert client.get(f"/api/sessions/{root['id']}/attachments").status_code == 404

    client.force_login(cook)
    assert client.delete(f"/api/attachments/{file['id']}").status_code == 200
    assert not ConversationSession.objects.get(id=root["id"]).attachments.exists()


@pytest.mark.django_db(transaction=True)
def test_a_queued_turn_returns_at_once(client, cook, use_bots, monkeypatch, settings):
    from ergonaut.apps.bots import tasks

    use_bots(say("hi"))
    root = post(client, "/api/bots/kitchen/root").json()
    queued = []
    monkeypatch.setattr(tasks.run_turn, "delay", lambda *args: queued.append(args) or object())
    settings.CELERY_TASK_ALWAYS_EAGER = False
    response = post(client, f"/api/sessions/{root['id']}/messages", {"text": "Eggs?"})
    assert response.status_code == 200
    assert response.json()["queued"] is True
    assert response.json()["text"] == ""
    assert queued == [(root["id"], "Eggs?", None, [])]


@pytest.mark.django_db(transaction=True)
def test_run_turn_answers_and_resumes(cook, use_bots):
    from django_ergo.conversation.models import ConversationSession

    from ergonaut.apps.bots.tasks import run_turn

    use_bots(say("Tacos."))
    session = ConversationSession.objects.create(user=cook, bot_name="kitchen", metadata={"bot_role": "root"})
    run_turn(str(session.id), "Dinner?")
    call = session.structured_calls.get()
    assert call.response["text"] == "Tacos."
    run_turn(str(session.id), approve=True)  # nothing waiting: a no-op
    assert session.structured_calls.count() == 1


@pytest.mark.django_db(transaction=True)
def test_files_sent_with_a_message_reach_the_model(client, cook, use_bots, settings, tmp_path):
    from django.core.files.uploadedfile import SimpleUploadedFile
    from django_ergo.conversation.models import ConversationSession

    settings.MEDIA_ROOT = str(tmp_path / "media")
    fake = use_bots(say("A fridge full of eggs."))
    root = post(client, "/api/bots/kitchen/root").json()
    photo = client.post(
        f"/api/sessions/{root['id']}/attachments",
        {"file": SimpleUploadedFile("fridge.png", b"\x89PNGfake", content_type="image/png")},
    ).json()
    sent = post(client, f"/api/sessions/{root['id']}/messages", {"text": "What's this?", "attachment_ids": [photo["id"]]})
    assert sent.status_code == 200, sent.content
    assert sent.json()["text"] == "A fridge full of eggs."
    first = fake.calls[0]["messages"]
    assert any(
        isinstance(m["content"], list) and any(p.get("type") == "image" for p in m["content"]) for m in first
    )
    session = ConversationSession.objects.get(id=root["id"])
    rows = list(session.attachments.values_list("filename", "source", "message_sequence"))
    assert len(rows) == 1 and rows[0][:2] == ("fridge.png", "message") and rows[0][2] is not None

    other = post(client, f"/api/sessions/{root['id']}/messages", {"text": "x", "attachment_ids": ["nope"]})
    assert other.status_code == 400


@pytest.mark.django_db(transaction=True)
def test_sessions_show_delegated_requests(client, cook, use_bots):
    from django_ergo.conversation.models import ConversationSession, ThreadMessage

    use_bots(say("hi"))
    root = post(client, "/api/bots/kitchen/root").json()
    parent = ConversationSession.objects.get(id=root["id"])
    thread = ConversationSession.objects.create(
        user=cook, bot_name="kitchen", parent=parent, metadata={"bot_role": "thread", "title": "Meals"}
    )
    ThreadMessage.objects.create(sender_session=parent, recipient_session=thread, text="Plan Tuesday", status="delivered")

    detail = client.get(f"/api/sessions/{root['id']}").json()
    assert detail["session"]["open_out"] == 1
    assert [(r["direction"], r["other"], r["status"]) for r in detail["requests"]] == [
        ("out", "kitchen · Meals", "delivered")
    ]
    listed = {s["id"]: s for s in client.get("/api/sessions").json()}
    assert (listed[str(thread.id)]["open_in"], listed[root["id"]]["open_out"]) == (1, 1)
    inside = client.get(f"/api/sessions/{thread.id}").json()
    assert [(r["direction"], r["other"]) for r in inside["requests"]] == [("in", "kitchen · Chat")]


@pytest.mark.django_db(transaction=True)
def test_changes_list_and_merge_need_a_manager_and_an_admin(client, cook, use_bots, monkeypatch):
    from ergonaut.api import bots as api

    use_bots(say("hi"))
    assert client.get("/api/bots/kitchen/changes").status_code == 404  # no bot_management

    class FakeManager:
        repo = "/repo"
        mode = "propose_pr"
        merged = []

        def draft_diff(self):
            return "+draft line"

        def pull_requests(self):
            return [{"number": 7, "title": "Add CTO", "url": "u", "headRefName": "b", "author": {"login": "bot"}}]

        def pull_request_diff(self, number):
            return f"diff for {number}"

        def merge_pull_request(self, number):
            self.merged.append(number)
            return f"Merged #{number}."

    fake = FakeManager()
    monkeypatch.setattr(api, "change_manager", lambda bot: (api.get_bot(bot), fake))
    changes = client.get("/api/bots/kitchen/changes").json()
    assert changes["draft_diff"] == "+draft line"
    assert [(p["number"], p["author"]) for p in changes["pull_requests"]] == [(7, "bot")]
    assert client.get("/api/bots/kitchen/changes/7/diff").json() == {"diff": "diff for 7"}

    assert post(client, "/api/bots/kitchen/changes/7/merge").status_code == 403
    cook.is_superuser = True
    cook.save()
    assert post(client, "/api/bots/kitchen/changes/7/merge").json() == {"result": "Merged #7."}
    assert fake.merged == [7]
