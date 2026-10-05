import json
import textwrap

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone
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
            "icon": "",
            "color": "",
            "orchestration": False,
            "knowledge": False,
            "parent": "",
            "root_session_id": None,
            "chats": [{"name": "main", "description": "", "session_id": None}],
        }
    ]
    root = post(client, "/api/bots/kitchen/root").json()
    assert root["role"] == "main"
    assert root["title"] == "Main"
    assert client.get("/api/bots").json()[0]["chats"][0]["session_id"] == root["id"]

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
    assert {"pantry_count", "order"} <= set(tools)
    assert "send_reply" not in tools  # the reply format, not a tool
    assert not [name for name in tools if name.startswith("ergo_thread")]  # orchestration is off
    assert tools["order"]["requires_approval"] is True
    assert tools["pantry_count"]["requires_approval"] is False
    skills = {s["name"]: s for s in bot["skills"]}
    assert skills["history"]["always_in"] == ["main", "threads"]
    assert skills["pantry"]["source"] == "tools/pantry.py"
    assert [t["name"] for t in skills["pantry"]["tools"]] == ["pantry_count", "order"]
    assert skills["meal-planning"]["body"] == "Look back 60 days."
    assert skills["meal-planning"]["always_in"] == []

    refused = post(client, "/api/bots/kitchen/threads", {"title": "Nope"})
    assert refused.status_code == 409

    root = post(client, "/api/bots/kitchen/root").json()
    post(client, f"/api/sessions/{root['id']}/messages", {"text": "hi"})
    [call] = client.get(f"/api/sessions/{root['id']}").json()["calls"]
    assert "ergo_skill_load" in call["tools"]
    assert "pantry_count" not in call["tools"]  # a skill, loaded when needed
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
        return browser.post(
            url, json.dumps(data or {}), content_type="application/json", headers={"X-CSRFToken": token}
        )

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
    from django.core.files.base import ContentFile
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
        "pantry.csv",
        "upload",
        "text/csv",
        18,
    )
    listed = client.get(f"/api/sessions/{root['id']}/attachments").json()
    assert [f["id"] for f in listed] == [file["id"]]
    assert listed[0]["archived_at"] is None
    # A file the bot archived is still listed, marked, for the Files panel's toggle.
    ConversationSession.objects.get(id=root["id"]).attachments.update(archived_at=timezone.now())
    listed = client.get(f"/api/sessions/{root['id']}/attachments").json()
    assert [f["id"] for f in listed] == [file["id"]]
    assert listed[0]["archived_at"]
    download = client.get(f"/api/attachments/{file['id']}/download")
    assert b"".join(download.streaming_content) == b"item,count\neggs,4\n"

    # A row whose file is gone (wiped media) is a 404 that says so, not a 500.
    row = ConversationSession.objects.get(id=root["id"]).attachments.get()
    content = row.file.read()
    row.file.close()
    row.file.storage.delete(row.file.name)
    missing = client.get(f"/api/attachments/{file['id']}/download?inline=true")
    assert missing.status_code == 404
    assert "missing" in missing.json()["detail"]
    row.file.storage.save(row.file.name, ContentFile(content))

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
    # The message waits in the session's inbox for the queued turn.
    assert queued == [(root["id"], None, None, [], None)]
    assert [(i["text"], i["attachment_ids"]) for i in tasks.drain_inbox(root["id"])] == [("Eggs?", [])]


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
    sent = post(
        client, f"/api/sessions/{root['id']}/messages", {"text": "What's this?", "attachment_ids": [photo["id"]]}
    )
    assert sent.status_code == 200, sent.content
    assert sent.json()["text"] == "A fridge full of eggs."
    first = fake.calls[0]["messages"]
    assert any(isinstance(m["content"], list) and any(p.get("type") == "image" for p in m["content"]) for m in first)
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
    ThreadMessage.objects.create(
        sender_session=parent, recipient_session=thread, text="Plan Tuesday", status="delivered"
    )

    detail = client.get(f"/api/sessions/{root['id']}").json()
    assert detail["session"]["open_out"] == 1
    assert [(r["direction"], r["other"], r["status"]) for r in detail["requests"]] == [
        ("out", "kitchen · Meals", "delivered")
    ]
    listed = {s["id"]: s for s in client.get("/api/sessions").json()}
    assert (listed[str(thread.id)]["open_in"], listed[root["id"]]["open_out"]) == (1, 1)
    inside = client.get(f"/api/sessions/{thread.id}").json()
    assert [(r["direction"], r["other"]) for r in inside["requests"]] == [("in", "kitchen · Main")]


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
    monkeypatch.setattr(api, "change_manager", lambda bot, user=None: (api.get_bot(bot), fake))
    changes = client.get("/api/bots/kitchen/changes").json()
    assert changes["draft_diff"] == "+draft line"
    assert [(p["number"], p["author"]) for p in changes["pull_requests"]] == [(7, "bot")]
    assert client.get("/api/bots/kitchen/changes/7/diff").json() == {"diff": "diff for 7"}

    assert post(client, "/api/bots/kitchen/changes/7/merge").status_code == 403
    cook.is_superuser = True
    cook.save()
    assert post(client, "/api/bots/kitchen/changes/7/merge").json() == {"result": "Merged #7."}
    assert fake.merged == [7]


@pytest.mark.django_db(transaction=True)
def test_stale_approvals_bad_ids_and_bot_access(client, cook, bot_folder, use_bots):
    from django_ergo.conversation.models import ConversationSession

    use_bots(tool_call("order", {"item": "milk"}), say("Ordered milk."))
    root = post(client, "/api/bots/kitchen/root").json()
    turn = post(client, f"/api/sessions/{root['id']}/messages", {"text": "Order milk"}).json()
    shown = [a["id"] for a in turn["approvals"]]
    stale = post(client, f"/api/sessions/{root['id']}/approvals", {"approve": True, "approval_ids": ["old"]})
    assert stale.status_code == 409
    ok = post(client, f"/api/sessions/{root['id']}/approvals", {"approve": True, "approval_ids": shown})
    assert ok.json()["text"] == "Ordered milk."

    assert client.get("/api/sessions/not-a-uuid").status_code == 404
    assert post(client, "/api/sessions/not-a-uuid/messages", {"text": "hi"}).status_code == 404
    assert client.get("/api/attachments/nope/download").status_code == 404

    # permissions.users limits who may use a bot.
    (bot_folder / "bot.yaml").write_text(BOT + "permissions: {users: [someone-else]}\n")
    use_bots(say("hi"))
    assert client.get("/api/bots/kitchen").status_code == 404
    assert [b["name"] for b in client.get("/api/bots").json()] == []
    assert ConversationSession.objects.filter(bot_name="kitchen").count() == 1


@pytest.mark.django_db(transaction=True)
def test_a_turn_that_fails_early_is_recorded(cook, use_bots, monkeypatch):
    from django_ergo.conversation.models import ConversationSession

    from ergonaut.apps.bots.tasks import run_turn

    use_bots(say("never"))
    session = ConversationSession.objects.create(user=cook, bot_name="kitchen", metadata={"bot_role": "root"})
    from django_ergo.bots.runtime import Bot

    def broken(self, *args, **kwargs):
        msg = "no API key"
        raise RuntimeError(msg)

    monkeypatch.setattr(Bot, "make_engine", broken)
    run_turn(str(session.id), "Dinner?")
    call = session.structured_calls.get()
    assert (call.status, call.request) == ("failed", "Dinner?")
    assert "no API key" in call.error


@pytest.mark.django_db(transaction=True)
def test_bot_page_lists_schedules(client, cook, bot_folder, use_bots):
    (bot_folder / "bot.yaml").write_text(
        BOT + 'schedules:\n  - {name: plan, cron: "0 17 * * sun", message: Plan dinners, to: new}\n'
    )
    use_bots(say("hi"))
    [schedule] = client.get("/api/bots/kitchen").json()["schedules"]
    assert (schedule["name"], schedule["cron"], schedule["enabled"]) == ("plan", "0 17 * * sun", True)
    [action] = schedule["actions"]
    assert (action["kind"], action["to"], action["thread_title"]) == ("prompt", "thread", "plan %b %d")
    assert schedule["next_run"]


@pytest.mark.django_db(transaction=True)
def test_named_chats_open_from_the_api(client, cook, bot_folder, use_bots):
    (bot_folder / "bot.yaml").write_text(BOT + "chats:\n  reports: {description: Weekly reports}\n")
    use_bots(say("hi"))
    [bot] = client.get("/api/bots").json()
    assert [(c["name"], c["session_id"]) for c in bot["chats"]] == [("main", None), ("reports", None)]
    opened = post(client, "/api/bots/kitchen/chats/reports").json()
    assert (opened["role"], opened["title"]) == ("chat", "Weekly reports")
    assert post(client, "/api/bots/kitchen/chats/reports").json()["id"] == opened["id"]
    assert post(client, "/api/bots/kitchen/chats/nope").status_code == 404
    [bot] = client.get("/api/bots").json()
    assert bot["chats"][1]["session_id"] == opened["id"]


@pytest.mark.django_db(transaction=True)
def test_pins_bot_files_and_live_pages(client, cook, bot_folder, use_bots, settings, tmp_path):
    from django_ergo.conversation.attachments import save_session_file
    from django_ergo.conversation.models import ConversationSession

    settings.MEDIA_ROOT = str(tmp_path / "media")
    (bot_folder / "bot.yaml").write_text(BOT + "chats:\n  main: {pins: [pages/home.jhtml]}\n")
    (bot_folder / "pages").mkdir()
    (bot_folder / "pages" / "home.jhtml").write_text("<p>Hello {{ user.username }} from {{ bot.name }}</p>")
    (bot_folder / "pages" / "app.mjs").write_text("export const x = 1\n")
    use_bots(say("hi"))
    root = post(client, "/api/bots/kitchen/root").json()
    session = ConversationSession.objects.get(id=root["id"])
    page = save_session_file(session, "board.jhtml", b"<b>{{ 6 * 7 }}</b>", source="bot", metadata={"title": "Board"})
    save_session_file(session, "evil.jhtml", b"{{ ''.__class__.__mro__ }}", source="bot")

    assert [p["url"] for p in client.get(f"/api/sessions/{root['id']}/pins").json()] == [
        "/api/bots/kitchen/files/pages/home.jhtml"
    ]
    pinned = post(client, f"/api/attachments/{page.id}/pin", {"pinned": True}).json()
    assert pinned["pinned"]
    pins = client.get(f"/api/sessions/{root['id']}/pins").json()
    assert [(p["kind"], p["name"]) for p in pins] == [("bot_file", "home.jhtml"), ("file", "Board")]
    assert [p["name"] for p in client.get("/api/pins").json()[root["id"]]] == ["home.jhtml", "Board"]

    # Pins take a title and an icon: in bot.yaml, and on a chat file.
    (bot_folder / "bot.yaml").write_text(
        BOT + 'chats:\n  main: {pins: [{path: pages/home.jhtml, title: Home, icon: "🏡"}]}\n'
    )
    use_bots(say("hi"))
    post(client, f"/api/attachments/{page.id}/pin", {"pinned": True, "icon": "📋"})
    labelled = client.get(f"/api/sessions/{root['id']}/pins").json()
    assert [(p["name"], p["icon"], p.get("path")) for p in labelled] == [
        ("Home", "🏡", "pages/home.jhtml"),
        ("Board", "📋", None),
    ]
    assert [(p["name"], p["icon"], p["filename"]) for p in client.get("/api/pins").json()[root["id"]]] == [
        ("Home", "🏡", "home.jhtml"),
        ("Board", "📋", "board.jhtml"),
    ]

    # A bot-folder page renders in the app's origin; assets come as files; code and config don't.
    home = client.get("/api/bots/kitchen/files/pages/home.jhtml")
    assert b"Hello cook from kitchen" in home.content
    assert "Content-Security-Policy" not in home and home["X-Frame-Options"] == "SAMEORIGIN"
    assert client.get("/api/bots/kitchen/files/pages/app.mjs")["Content-Type"] == "text/javascript"
    for blocked in ("bot.yaml", "tools/pantry.py", "../kitchen/bot.yaml", "pages/nope.jhtml"):
        assert client.get(f"/api/bots/kitchen/files/{blocked}").status_code == 404

    # A page the bot wrote renders live, sandboxed into its own origin.
    board = client.get(pins[1]["url"])
    assert b"<b>42</b>" in board.content
    assert board["Content-Security-Policy"].startswith("sandbox allow-scripts")
    evil = client.get(f"/api/attachments/{session.attachments.get(filename='evil.jhtml').id}/download?inline=true")
    assert b"couldn&#39;t render" in evil.content or b"couldn't render" in evil.content

    other = get_user_model().objects.create_user("other", "o@example.com", "pw")
    client.force_login(other)
    assert client.get(f"/api/sessions/{root['id']}/pins").status_code == 404
    assert client.get(pins[1]["url"]).status_code == 404


@pytest.mark.django_db(transaction=True)
def test_files_open_in_the_viewer_by_kind(client, cook, use_bots, settings, tmp_path):
    from django_ergo.conversation.attachments import save_session_file
    from django_ergo.conversation.models import ConversationSession

    settings.MEDIA_ROOT = str(tmp_path / "media")
    use_bots(say("hi"))
    root = post(client, "/api/bots/kitchen/root").json()
    session = ConversationSession.objects.get(id=root["id"])
    files = {
        "notes.md": b"# Plan\n\n<script>x</script> **bold**",
        "pantry.csv": b"item,count\neggs,4\n",
        "data.json": b'{"a": [1, 2]}',
        "photo.png": b"\x89PNG\r\n",
        "drawing.svg": b"<svg xmlns='http://www.w3.org/2000/svg'/>",
        "receipt.pdf": b"%PDF-1.4",
        "blob.bin": b"\x00\x01",
    }
    rows = {name: save_session_file(session, name, data, source="upload") for name, data in files.items()}
    listed = {f["filename"]: f["view"] for f in client.get(f"/api/sessions/{root['id']}/attachments").json()}
    assert listed == {
        "notes.md": "markdown",
        "pantry.csv": "csv",
        "data.json": "json",
        "photo.png": "image",
        "drawing.svg": "image",
        "receipt.pdf": "pdf",
        "blob.bin": "",
    }

    def view(name):
        return client.get(f"/api/attachments/{rows[name].id}/download?inline=true")

    md = view("notes.md")
    assert b"<h1>Plan</h1>" in md.content and b"<strong>bold</strong>" in md.content
    assert b"<script>x</script>" not in md.content
    assert md["Content-Security-Policy"].startswith("sandbox")
    assert b"<td>eggs</td>" in view("pantry.csv").content
    assert b"&#34;a&#34;: [\n    1" in view("data.json").content  # pretty-printed, escaped
    for name in ("photo.png", "receipt.pdf"):
        response = view(name)
        assert "attachment" not in response.get("Content-Disposition", "")
        assert response["X-Frame-Options"] == "SAMEORIGIN"
    assert view("drawing.svg")["Content-Security-Policy"].startswith("sandbox")
    assert "attachment" in view("blob.bin")["Content-Disposition"]


@pytest.mark.django_db(transaction=True)
def test_admins_browse_the_bot_folder(client, cook, bot_folder, use_bots):
    (bot_folder / ".env").write_text("TOKEN=x")
    (bot_folder / "tools" / "__pycache__").mkdir()
    (bot_folder / "tools" / "__pycache__" / "pantry.pyc").write_bytes(b"\x00")
    (bot_folder / "logo.png").write_bytes(b"\x89PNG\r\n\x00")
    use_bots(say("hi"))
    assert client.get("/api/bots/kitchen/tree").status_code == 403

    cook.is_superuser = True
    cook.save()
    paths = [f["path"] for f in client.get("/api/bots/kitchen/tree").json()["files"]]
    assert paths == ["agents.md", "bot.yaml", "logo.png", "tools/pantry.py"]

    source = client.get("/api/bots/kitchen/source/tools/pantry.py").json()
    assert "def pantry_count" in source["text"] and source["url"] == ""
    logo = client.get("/api/bots/kitchen/source/logo.png").json()
    assert logo["text"] is None and logo["url"] == "/api/bots/kitchen/files/logo.png"
    for blocked in (".env", "../kitchen/.env", "tools/__pycache__/pantry.pyc", "nope.py"):
        assert client.get(f"/api/bots/kitchen/source/{blocked}").status_code == 404


@pytest.mark.django_db(transaction=True)
def test_files_show_the_draft_and_pull_requests_with_diffs(client, cook, bot_folder, use_bots, tmp_path, monkeypatch):
    import subprocess

    from ergonaut.api import bots as api

    for key in ("GIT_AUTHOR_NAME", "GIT_COMMITTER_NAME"):
        monkeypatch.setenv(key, "Test")
    for key in ("GIT_AUTHOR_EMAIL", "GIT_COMMITTER_EMAIL"):
        monkeypatch.setenv(key, "test@example.com")

    def git(cwd, *args):
        return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout

    (bot_folder / "bot.yaml").write_text(BOT + "plugins:\n  - {name: bot_management, mode: propose_pr}\n")
    remote = tmp_path / "remote.git"
    git(tmp_path, "init", "--bare", "-b", "main", str(remote))
    git(bot_folder, "init", "-b", "main")
    git(bot_folder, "add", "-A")
    git(bot_folder, "commit", "-m", "init")
    git(bot_folder, "remote", "add", "origin", str(remote))
    git(bot_folder, "push", "-u", "origin", "main")
    # A pull request: another clone pushes a branch to refs/pull/9/head, as GitHub does.
    other = tmp_path / "other"
    git(tmp_path, "clone", str(remote), str(other))
    (other / "agents.md").write_text("You run the kitchen. Be brief.")
    git(other, "commit", "-am", "brief")
    git(other, "push", "origin", "HEAD:refs/pull/9/head")

    use_bots(say("hi"))
    cook.is_superuser = True
    cook.save()
    _, plugin = api.change_manager("kitchen", cook)
    plugin.write("tools/pantry.py", (bot_folder / "tools" / "pantry.py").read_text() + "\n# draft edit\n")
    plugin.write("notes.md", "new file")
    monkeypatch.setattr(
        type(plugin),
        "pull_requests",
        lambda self: [{"number": 9, "title": "Be brief", "url": "u", "files": [{"path": "agents.md"}]}],
    )

    proposals = client.get("/api/bots/kitchen/proposals").json()["proposals"]
    assert [(p["version"], p["changed"]) for p in proposals] == [
        ("draft", {"notes.md": "A", "tools/pantry.py": "M"}),
        ("pr-9", {"agents.md": "M"}),
    ]

    plugin.diff()  # marks new files --intent-to-add, as the bot's own diff does
    assert client.get("/api/bots/kitchen/proposals").json()["proposals"][0]["changed"]["notes.md"] == "A"
    draft = {f["path"]: f["status"] for f in client.get("/api/bots/kitchen/tree?version=draft").json()["files"]}
    assert draft["tools/pantry.py"] == "M" and draft["notes.md"] == "A" and draft["agents.md"] == ""
    source = client.get("/api/bots/kitchen/source/tools/pantry.py?version=draft").json()
    assert "+# draft edit" in source["diff"] and "# draft edit" in source["text"]
    added = client.get("/api/bots/kitchen/source/notes.md?version=draft").json()
    assert added["diff"].startswith("--- /dev/null")

    pr = {f["path"]: f["status"] for f in client.get("/api/bots/kitchen/tree?version=pr-9").json()["files"]}
    assert pr["agents.md"] == "M" and "notes.md" not in pr
    brief = client.get("/api/bots/kitchen/source/agents.md?version=pr-9").json()
    assert "-You run the kitchen." in brief["diff"] and "+You run the kitchen. Be brief." in brief["diff"]

    # The live view is untouched, and versions are checked.
    assert "# draft edit" not in client.get("/api/bots/kitchen/source/tools/pantry.py").json()["text"]
    assert client.get("/api/bots/kitchen/tree?version=../x").status_code == 400


@pytest.mark.django_db(transaction=True)
def test_a_thread_started_from_a_message_gets_a_generated_title(client, cook, bot_folder, use_bots, monkeypatch):
    from django_ergo.conversation.models import ConversationSession

    from ergonaut.apps.bots import tasks

    (bot_folder / "bot.yaml").write_text(textwrap.dedent(BOT).replace("orchestration: false", "orchestration: true"))
    use_bots(tool_call("submit_output", {"title": "Weekly grocery order"}))
    monkeypatch.setattr(tasks, "queue_thread_naming", tasks.name_thread)  # run it now
    message = "Can you put together the grocery order for this week, the usual plus oat milk?"
    thread = post(client, "/api/bots/kitchen/threads", {"message": message}).json()
    assert thread["parent_id"] is not None
    # Created with a provisional title from the message, then named by the model.
    session = ConversationSession.objects.get(id=thread["id"])
    assert session.metadata["title"] == "Weekly grocery order"
    assert session.metadata["bot_role"] == "thread"
    from django_ergo.conversation.models import StructuredCall

    naming = StructuredCall.objects.get(kind="new_thread_metadata")
    assert (naming.status, naming.request) == ("completed", message)
    assert post(client, "/api/bots/kitchen/threads", {"title": "Groceries"}).json()["title"] == "Groceries"
    assert StructuredCall.objects.filter(kind="new_thread_metadata").count() == 1  # a given title isn't regenerated


@pytest.mark.django_db(transaction=True)
def test_sessions_say_when_a_turn_is_running(client, cook, use_bots):
    from django.utils import timezone
    from django_ergo.conversation.models import StructuredCall

    use_bots(say("hi"))
    root = post(client, "/api/bots/kitchen/root").json()

    def busy():
        return {s["id"]: s["busy"] for s in client.get("/api/sessions").json()}[root["id"]]

    assert busy() is False
    call = StructuredCall.objects.create(kind="chat_reply", session_id=root["id"], user=cook, status="in_progress")
    assert busy() is True
    # A call stuck "in progress" for longer than any turn is a crashed worker, not a busy chat.
    StructuredCall.objects.filter(pk=call.pk).update(updated_at=timezone.now() - timezone.timedelta(hours=1))
    assert busy() is False
    StructuredCall.objects.filter(pk=call.pk).update(status="completed", updated_at=timezone.now())
    assert busy() is False


@pytest.mark.django_db(transaction=True)
def test_running_workers_show_in_the_chat_and_keep_it_busy(client, cook, use_bots, monkeypatch):
    from django.utils import timezone
    from django_ergo.conversation.models import Worker

    from ergonaut.apps.bots import tasks

    use_bots(say("hi"))
    root = post(client, "/api/bots/kitchen/root").json()
    worker = Worker.objects.create(
        session_id=root["id"],
        bot_name="kitchen",
        title="Fix footer",
        function="orca:watch",
        status="running",
        progress="running · alive",
        state={"activity": {"entries": [{"kind": "tool", "text": "Bash pytest", "at": None}], "at": 1790000000}},
    )
    assert {s["id"]: s["busy"] for s in client.get("/api/sessions").json()}[root["id"]] is True
    detail = client.get(f"/api/sessions/{root['id']}").json()
    assert [(w["title"], w["status"], w["progress"]) for w in detail["workers"]] == [
        ("Fix footer", "running", "running · alive")
    ]
    assert detail["workers"][0]["session_id"] == root["id"]
    assert detail["workers"][0]["activity"]["at"] == 1790000000

    # Its log: this bot has no Orca plugin to read it from now, so it's what was kept.
    log = client.get(f"/api/sessions/{root['id']}/workers/{worker.pk}/log").json()
    assert (log["live"], log["entries"][0]["text"]) == (False, "Bash pytest")
    assert client.get(f"/api/sessions/{root['id']}/workers/{root['id']}/log").status_code == 404

    # A worker whose next step is long overdue (a restart lost it) is started again.
    started = []
    monkeypatch.setattr(tasks, "queue_worker", lambda worker_id, delay: started.append(worker_id))
    from django.test import override_settings

    with override_settings(DJANGO_ERGO={"WORKER_RUNNER": "ergonaut.apps.bots.tasks.queue_worker"}):
        Worker.objects.filter(pk=worker.pk).update(next_poll_at=timezone.now() - timezone.timedelta(minutes=10))
        assert tasks.resume_workers() == 1
    Worker.objects.filter(pk=worker.pk).update(status="completed")
    assert {s["id"]: s["busy"] for s in client.get("/api/sessions").json()}[root["id"]] is False


@pytest.mark.django_db(transaction=True)
def test_browse_a_bot_tables_rows(client, cook, use_bots, monkeypatch):
    from django_ergo.bots.runtime import Bot
    from django_ergo.conversation.models import BotJob

    use_bots(say("hi"))
    # Any model stands in for a bot table here.
    monkeypatch.setattr(Bot, "table", lambda self, name: BotJob if name.lower() == "botjob" else Bot.__dict__["nope"])
    for i in range(7):
        BotJob.objects.create(
            bot_name="kitchen", name=f"job {i}", target="tools/x.py:pull" if i % 2 else "tools/y.py:push"
        )
    first = client.get("/api/bots/kitchen/tables/BotJob/rows?page_size=3&order=-name").json()
    assert (first["count"], len(first["rows"]), first["page_size"]) == (7, 3, 3)
    assert [r["name"] for r in first["rows"]] == ["job 6", "job 5", "job 4"]
    assert {"name": "name", "type": "CharField"} in first["fields"]
    last = client.get("/api/bots/kitchen/tables/BotJob/rows?page_size=3&page=3&order=name").json()
    assert [r["name"] for r in last["rows"]] == ["job 6"]
    found = client.get("/api/bots/kitchen/tables/BotJob/rows?q=y.py").json()
    assert found["count"] == 4
    assert client.get("/api/bots/kitchen/tables/BotJob/rows?order=no_such_field").status_code == 200
    monkeypatch.setattr(Bot, "table", lambda self, name: (_ for _ in ()).throw(LookupError("kitchen has no table")))
    assert client.get("/api/bots/kitchen/tables/Nope/rows").status_code == 404


@pytest.mark.django_db(transaction=True)
def test_sessions_show_unread_replies_and_what_needs_attention(client, cook, use_bots):
    from django_ergo.conversation.models import ConversationSession

    use_bots(say("You have 4 eggs."), say("Which store?", ["Safeway", "Costco"], kind="question"))
    root = post(client, "/api/bots/kitchen/root").json()

    def listed():
        s = {x["id"]: x for x in client.get("/api/sessions").json()}[root["id"]]
        return s["unread"], s["attention"]

    assert listed() == (False, False)
    post(client, f"/api/sessions/{root['id']}/messages", {"text": "How many eggs?"})
    # A reply the owner hasn't opened yet is unread; opening the chat reads it.
    ConversationSession.objects.filter(id=root["id"]).update(read_at="2000-01-01T00:00Z")
    assert listed() == (True, False)
    client.get(f"/api/sessions/{root['id']}")
    assert listed() == (False, False)

    # A question waits on the user until they answer it.
    post(client, f"/api/sessions/{root['id']}/messages", {"text": "Buy more"})
    client.get(f"/api/sessions/{root['id']}")
    assert listed() == (False, True)


@pytest.mark.django_db(transaction=True)
def test_threads_are_grouped_by_status_with_the_bots_status_line(client, cook, use_bots):
    from django_ergo.conversation.links import GITHUB_PR
    from django_ergo.conversation.models import ConversationAttachment, ConversationSession, StructuredCall

    use_bots(
        say("On it.", status="Checking the pantry"),
        say("Which store?", ["Safeway", "Costco"], kind="question", status="Pick a store"),
    )
    root = post(client, "/api/bots/kitchen/root").json()

    def listed(session_id=root["id"]):
        return {s["id"]: s for s in client.get("/api/sessions").json()}[session_id]

    assert (listed()["bucket"], listed()["status_line"]) == ("idle", "")
    post(client, f"/api/sessions/{root['id']}/messages", {"text": "How many eggs?"})
    assert (listed()["bucket"], listed()["status_line"]) == ("idle", "Checking the pantry")
    post(client, f"/api/sessions/{root['id']}/messages", {"text": "Buy more"})
    s = listed()
    assert (s["bucket"], s["waiting_for"], s["status_line"]) == ("waiting", "question", "Pick a store")

    # A failed turn waits on the user too, and a running one is working.
    StructuredCall.objects.create(kind="chat_reply", session_id=root["id"], user=cook, status="failed")
    assert (listed()["bucket"], listed()["waiting_for"]) == ("waiting", "failure")
    StructuredCall.objects.create(kind="chat_reply", session_id=root["id"], user=cook, status="in_progress")
    assert (listed()["bucket"], listed()["waiting_for"]) == ("working", "")

    # A resolved thread shows its summary; its pull requests come along as chips.
    thread = ConversationSession.objects.create(
        user=cook,
        bot_name="kitchen",
        parent_id=root["id"],
        status="completed",
        metadata={"bot_role": "thread", "title": "Build it", "resolved_summary": "Shipped in PR 40"},
    )
    ConversationAttachment.objects.create(
        session=thread,
        url="https://github.com/acme/app/pull/40",
        metadata={"link": GITHUB_PR, "repo": "acme/app", "number": 40, "state": "merged"},
    )
    # A quiet thread with an open pull request is ready for review.
    review = ConversationSession.objects.create(
        user=cook, bot_name="kitchen", parent_id=root["id"], metadata={"bot_role": "thread", "title": "Fix it"}
    )
    ConversationAttachment.objects.create(
        session=review,
        url="https://github.com/acme/app/pull/41",
        metadata={"link": GITHUB_PR, "repo": "acme/app", "number": 41, "state": "open"},
    )
    assert listed(str(review.id))["bucket"] == "review"

    s = listed(str(thread.id))
    assert (s["bucket"], s["status_line"]) == ("resolved", "Shipped in PR 40")
    assert [(pr["number"], pr["state"]) for pr in s["prs"]] == [(40, "merged")]

    # Pinning doesn't count as activity.
    before = listed(str(thread.id))["updated_at"]
    assert post(client, f"/api/sessions/{thread.id}/pin", {"pinned": True}).json()["pinned"] is True
    assert (listed(str(thread.id))["pinned"], listed(str(thread.id))["updated_at"]) == (True, before)
    assert post(client, f"/api/sessions/{thread.id}/pin", {"pinned": False}).json()["pinned"] is False


@pytest.mark.django_db(transaction=True)
def test_chats_pick_a_model_from_providers(client, cook, use_bots, monkeypatch):
    from django_ergo.bots.providers import Providers

    use_bots(say("hi"))
    webhooks.get_registry().providers = Providers.from_dict(
        {
            "providers": {
                "anthropic": {"type": "claude", "api_key_env": "TEST_CLAUDE_KEY", "models": ["claude-opus-5-5"]},
                "spare": {"type": "claude", "api_key_env": "TEST_UNSET_KEY", "models": ["claude-haiku-4-5"]},
                "openai": {"type": "openai", "models": ["gpt-6-sol"]},
            }
        }
    )
    monkeypatch.setenv("TEST_CLAUDE_KEY", "k")
    monkeypatch.delenv("TEST_UNSET_KEY", raising=False)

    models = client.get("/api/bots/kitchen/models").json()
    assert [(m["id"], m["engine_type"], m["available"]) for m in models["models"]] == [
        ("anthropic/claude-opus-5-5", "claude", True),
        ("spare/claude-haiku-4-5", "claude", False),
        ("openai/gpt-6-sol", "openai", True),
    ]
    root = post(client, "/api/bots/kitchen/root").json()
    assert (root["engine_type"], root["model"]) == ("claude", "")

    picked = post(client, f"/api/sessions/{root['id']}/model", {"model": "anthropic/claude-opus-5-5"})
    assert picked.json()["model"] == "anthropic/claude-opus-5-5"
    assert post(client, f"/api/sessions/{root['id']}/model", {"model": "spare/claude-haiku-4-5"}).status_code == 409
    assert post(client, f"/api/sessions/{root['id']}/model", {"model": "nope/x"}).status_code == 400

    # A model on another engine takes the next turn; the history stays as it is.
    from django_ergo.conversation.models import ConversationSession, MessageBlock, SessionMessage

    session = ConversationSession.objects.get(id=root["id"])
    asked = SessionMessage.objects.create(session=session, role="user", sequence=0)
    MessageBlock.objects.create(message=asked, block_type="text", sequence=0, text="Dinner?")
    moved = post(client, f"/api/sessions/{root['id']}/model", {"model": "openai/gpt-6-sol"}).json()
    assert (moved["engine_type"], moved["model"]) == ("openai", "openai/gpt-6-sol")
    assert [m.content_blocks.get().text for m in session.messages.all()] == ["Dinner?"]

    # Even while a turn runs (it finishes on its model); "" goes back to the bot's default.
    monkeypatch.setattr("ergonaut.apps.bots.tasks.turn_running", lambda session_id: True)
    back = post(client, f"/api/sessions/{root['id']}/model", {"model": ""}).json()
    assert (back["engine_type"], back["model"]) == ("claude", "")


@pytest.mark.django_db(transaction=True)
def test_a_failed_turn_explains_itself_and_can_be_resumed_or_dismissed(client, cook, use_bots):
    from django_ergo.conversation.models import StructuredCall

    use_bots(say("Picked up where I left off."))
    root = post(client, "/api/bots/kitchen/root").json()
    failed = StructuredCall.objects.create(
        kind="chat_reply",
        session_id=root["id"],
        user=cook,
        request="Plan dinner",
        status="failed",
        error="API call failed: Error code: 429 - {'error': {'message': 'You have no credits remaining.'}}",
    )

    def listed():
        return {s["id"]: s for s in client.get("/api/sessions").json()}[root["id"]]["attention"]

    call = client.get(f"/api/sessions/{root['id']}").json()["calls"][-1]
    assert call["error_summary"] == "The model provider's account is out of credits."
    assert call["error_hint"] == "Add credits to the account, then Resume."
    assert listed() is True

    assert post(client, f"/api/calls/{failed.id}/dismiss").json()["dismissed"] is True
    assert listed() is False

    turn = post(client, f"/api/sessions/{root['id']}/resume").json()
    assert turn["text"] == "Picked up where I left off."
    latest = StructuredCall.objects.filter(session_id=root["id"]).latest("created_at")
    assert latest.request.startswith("[Resume] Your last turn stopped before it finished (")
    assert "out of credits" in latest.request
    assert post(client, f"/api/sessions/{root['id']}/resume").status_code == 409  # it didn't fail this time


@pytest.mark.django_db(transaction=True)
def test_a_long_chat_comes_a_page_at_a_time(client, cook, use_bots):
    from django_ergo.conversation.models import ConversationSession, MessageBlock, SessionMessage, StructuredCall

    use_bots(say("hi"))
    session = ConversationSession.objects.create(
        user=cook, bot_name="kitchen", engine_type="openai", metadata={"bot_role": "root"}
    )
    for n in range(120):
        message = SessionMessage.objects.create(session=session, role="user" if n % 2 == 0 else "assistant", sequence=n)
        MessageBlock.objects.create(message=message, block_type="text", sequence=0, text=f"m{n}")
    old = StructuredCall.objects.create(
        kind="chat_reply", session=session, status="completed", first_sequence=0, last_sequence=1
    )
    span = StructuredCall.objects.create(
        kind="chat_reply", session=session, status="completed", first_sequence=60, last_sequence=75
    )
    new = StructuredCall.objects.create(
        kind="chat_reply", session=session, status="completed", first_sequence=118, last_sequence=119
    )

    page = client.get(f"/api/sessions/{session.id}").json()
    assert [m["line"] for m in page["messages"]] == list(range(70, 120))
    assert (page["first_line"], page["has_more"], page["message_count"]) == (70, True, 120)
    assert {c["id"] for c in page["calls"]} == {str(span.id), str(new.id)}

    older = client.get(f"/api/sessions/{session.id}?before=70").json()
    assert [m["line"] for m in older["messages"]] == list(range(20, 70))
    assert (older["first_line"], older["has_more"]) == (20, True)
    assert {c["id"] for c in older["calls"]} == {str(span.id)}

    oldest = client.get(f"/api/sessions/{session.id}?before=20").json()
    assert [m["line"] for m in oldest["messages"]] == list(range(20))
    assert (oldest["first_line"], oldest["has_more"]) == (0, False)
    assert {c["id"] for c in oldest["calls"]} == {str(old.id)}


@pytest.mark.django_db(transaction=True)
def test_thread_cards_and_pull_requests(client, cook, use_bots):
    from django_ergo.conversation.links import record_pull_requests
    from django_ergo.conversation.models import ConversationSession, ThreadMessage

    use_bots(say("Opened https://github.com/boundcorp/ergo-bots/pull/40 for it."))
    root = post(client, "/api/bots/kitchen/root").json()
    sender = ConversationSession.objects.get(id=root["id"])
    thread = ConversationSession.objects.create(
        user=cook, bot_name="kitchen", parent=sender, metadata={"bot_role": "thread", "title": "Build it"}
    )
    message = ThreadMessage.objects.create(
        sender_session=sender, recipient_session=thread, text="Please build it", status="delivered"
    )
    record_pull_requests(thread, "https://github.com/boundcorp/django-ergo/pull/7")

    [card] = client.get(f"/api/sessions/{root['id']}").json()["sent"]
    assert (card["message_id"], card["status"], card["text"]) == (str(message.id), "working", "Please build it")
    assert (card["thread"]["id"], card["thread"]["title"], card["thread"]["state"]) == (
        str(thread.id),
        "Build it",
        "idle",
    )
    assert [(p["repo"], p["number"]) for p in card["prs"]] == [("boundcorp/django-ergo", 7)]

    message.status, message.reply_text = "answered", "Done: https://github.com/boundcorp/ergo-bots/pull/41"
    message.save()
    [card] = client.get(f"/api/sessions/{root['id']}").json()["sent"]
    assert card["status"] == "done"
    assert [p["number"] for p in card["prs"]] == [7, 41]

    # A reply that links a pull request records it in the chat, as a file with a link.
    post(client, f"/api/sessions/{root['id']}/messages", {"text": "Ship it"})
    detail = client.get(f"/api/sessions/{root['id']}").json()
    assert [(p["number"], p["state"]) for p in detail["prs"]] == [(40, "")]
    [file] = [f for f in client.get(f"/api/sessions/{root['id']}/attachments").json() if f["link"]]
    assert (file["filename"], file["view"], file["link"]["number"]) == ("ergo-bots#40", "", 40)


@pytest.mark.django_db(transaction=True)
def test_a_resolved_thread_shows_who_resolved_it_and_why(client, cook, use_bots):
    from django_ergo.conversation.models import ConversationSession, ThreadMessage

    use_bots(say("hi"))
    root = post(client, "/api/bots/kitchen/root").json()
    sender = ConversationSession.objects.get(id=root["id"])
    thread = ConversationSession.objects.create(
        user=cook,
        bot_name="kitchen",
        parent=sender,
        status="completed",
        metadata={
            "bot_role": "thread",
            "title": "Build it",
            "resolved_by": "kitchen · Main",
            "resolved_summary": "Shipped in PR 40",
        },
    )
    ThreadMessage.objects.create(sender_session=sender, recipient_session=thread, text="Build it", status="answered")

    detail = client.get(f"/api/sessions/{thread.id}").json()["session"]
    assert (detail["resolved_by"], detail["resolved_summary"]) == ("kitchen · Main", "Shipped in PR 40")
    [card] = client.get(f"/api/sessions/{root['id']}").json()["sent"]
    assert (card["thread"]["archived"], card["thread"]["resolved_summary"]) == (True, "Shipped in PR 40")

    # Reopened, it isn't resolved any more (even if the metadata lingers).
    thread.status = "active"
    thread.save()
    assert client.get(f"/api/sessions/{thread.id}").json()["session"]["resolved_summary"] == ""


@pytest.mark.django_db(transaction=True)
def test_routing_page_shows_tiers_and_admins_set_the_priorities(client, cook, use_bots, monkeypatch):
    from django_ergo.bots.providers import Providers
    from django_ergo.conversation.models import RoutingText

    use_bots(say("hi"))
    webhooks.get_registry().providers = Providers.from_dict(
        {
            "providers": {
                "openai": {"type": "openai", "models": ["gpt-6-sol", "gpt-6-luna"]},
            },
            "tiers": {"low": ["openai/gpt-6-luna"], "medium": ["openai/gpt-6-sol"]},
        }
    )
    compiles = []
    monkeypatch.setattr(
        "django_ergo.bots.routing.ensure_compiled", lambda providers, make_engine, retry=False: compiles.append(retry)
    )

    page = client.get("/api/routing").json()
    assert [(t["name"], t["picked"]) for t in page["tiers"]] == [
        ("low", "openai/gpt-6-luna"),
        ("medium", "openai/gpt-6-sol"),
    ]
    assert page["providers"][0]["status"] == "in_use"
    assert (page["text"], page["editable"]) == ("", False)

    text = {"text": "Lean on Claude until its 5-hour window is 85% used."}
    assert client.put("/api/routing", json.dumps(text), content_type="application/json").status_code == 403

    cook.is_superuser = True
    cook.save()
    saved = client.put("/api/routing", json.dumps(text), content_type="application/json").json()
    assert (saved["text"], saved["text_source"], saved["editable"]) == (text["text"], "page", True)
    assert RoutingText.objects.get().updated_by == cook
    assert compiles == [True]

    reset = client.delete("/api/routing").json()
    assert (reset["text"], reset["text_source"]) == ("", "")
    assert not RoutingText.objects.exists()


@pytest.mark.django_db(transaction=True)
def test_a_turn_refused_at_its_limit_offers_a_retry_on_another_model(client, cook, use_bots, monkeypatch):
    from django_ergo.bots.providers import Providers
    from django_ergo.conversation.models import ConversationSession, StructuredCall

    use_bots(say("Back on it."))
    webhooks.get_registry().providers = Providers.from_dict(
        {
            "providers": {
                "anthropic": {"type": "claude", "models": ["claude-opus-5-5"]},
                "openai": {"type": "openai", "models": ["gpt-6-sol"]},
            },
            "tiers": {"medium": ["anthropic/claude-opus-5-5", "openai/gpt-6-sol"]},
        }
    )
    root = post(client, "/api/bots/kitchen/root").json()
    assert post(client, f"/api/sessions/{root['id']}/model", {"model": "auto/medium"}).status_code == 200
    session = ConversationSession.objects.get(id=root["id"])
    session.metadata = {**session.metadata, "routed_model": "anthropic/claude-opus-5-5"}
    session.save()
    failed = StructuredCall.objects.create(
        kind="chat_reply",
        session_id=root["id"],
        user=cook,
        request="Plan dinner",
        status="failed",
        error="Claude Code failed: Claude AI usage limit reached|1791200000",
    )

    detail = client.get(f"/api/sessions/{root['id']}").json()
    assert detail["retry_model"] == "openai/gpt-6-sol"
    session.refresh_from_db()
    assert session.metadata["routed_model"] == "anthropic/claude-opus-5-5"  # nothing moved on its own

    assert post(client, f"/api/sessions/{root['id']}/resume?model=nope/x").status_code == 400
    turn = post(client, f"/api/sessions/{root['id']}/resume?model=openai/gpt-6-sol").json()
    assert turn["text"] == "Back on it."
    session.refresh_from_db()
    assert session.metadata["routed_model"] == "openai/gpt-6-sol"
    failed.refresh_from_db()
    assert failed.metadata["resumed"] is True
    assert client.get(f"/api/sessions/{root['id']}").json()["retry_model"] == ""
