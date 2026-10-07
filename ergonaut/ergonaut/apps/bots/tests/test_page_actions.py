import time

import pytest
from django.contrib.auth import get_user_model
from django_ergo.bots import page_actions
from django_ergo.conversation.models import ConversationSession, SessionMessage

from ergonaut.apps.bots.models import PageActionCall
from ergonaut.apps.bots.tests.pagefixtures import post, write_bot

BOT = """
    name: kitchen
    description: Runs the kitchen
    engine: {type: claude}
    orchestration: false
    tools: [tools/pantry.py]
    tables: [tables.py]
    permissions: {users: [cook, lee]}
"""

TABLES = """
    from django.db import models
    from django_ergo.bots import BotTable

    class Pantry(BotTable):
        name = models.CharField(max_length=100)
        on_order = models.IntegerField(default=0)
"""

TOOLS = """
    import time
    from django_ergo.bots import bot_tool, page_action

    @page_action(requires_approval=True, approval_preview=lambda ctx, item, qty: f"Order {qty} x {item}")
    def restock(ctx, item: str, qty: int = 1) -> dict:
        ctx.table("Pantry").objects.filter(name=item).update(on_order=qty)
        ctx.table("Pantry").touch()
        return {"message": f"Ordered {qty} {item}", "reload": True, "by": ctx.user.username, "page": ctx.page}

    @page_action
    def note(ctx, text: str) -> dict:
        ctx.table("Pantry").objects.create(name=text)
        return {"seen_from": ctx.session.id.hex if ctx.session else None}

    @page_action
    def refuse(ctx, why: str):
        raise ValueError(why)

    @page_action
    def explode(ctx):
        raise RuntimeError("secret detail")

    @page_action
    def slow(ctx):
        time.sleep(0.5)
        return {"done": True}

    @bot_tool
    def plain(item: str) -> str:
        return item
"""


@pytest.fixture
def kitchen(tmp_path, install):
    folder = write_bot(tmp_path / "kitchen", BOT, tables=TABLES, tools=TOOLS)
    registry, client = install(folder, "ergo_bot_kitchen")
    registry.get("kitchen").table("Pantry").objects.create(name="flour")
    return registry.get("kitchen")


def call(client, name, args=None, **body):
    return post(client, f"/api/bots/kitchen/actions/{name}", {"args": args if args is not None else {}, **body})


@pytest.mark.django_db(transaction=True)
def test_login_required_and_unknown_or_bad_calls(client, kitchen, cook):
    from django.test import Client

    assert call(Client(), "note", {"text": "x"}).status_code == 401
    assert call(client, "nope").status_code == 404
    assert call(client, "plain", {"item": "x"}).status_code == 404  # a plain tool isn't an action
    for args, message in [
        ({}, "missing text"),
        ({"text": 5}, "text must be string"),
        ({"text": "x", "extra": 1}, "unknown argument"),
        ([1], "args must be an object"),
    ]:
        response = call(client, "note", args)
        assert response.status_code == 400
        assert message in response.json()["detail"]
    assert post(client, "/api/bots/kitchen/actions/note", {"args": "text"}).status_code == 400
    assert PageActionCall.objects.count() == 0  # nothing ran
    assert kitchen.table("Pantry").objects.count() == 1


@pytest.mark.django_db(transaction=True)
def test_a_user_the_bot_isnt_for_cant_call_it(client, kitchen, cook):
    get_user_model().objects.create_user("stranger", "s@example.com", "pw")
    client.logout()
    client.post("/api/auth/login", {"username": "stranger", "password": "pw"}, content_type="application/json")
    assert call(client, "note", {"text": "x"}).status_code == 404
    assert kitchen.table("Pantry").objects.count() == 1


@pytest.mark.django_db(transaction=True)
def test_the_session_must_be_one_the_viewer_can_see(client, kitchen, cook):
    other = get_user_model().objects.create_user("lee", "l@example.com", "pw")
    theirs = ConversationSession.objects.create(user=other, bot_name="kitchen")
    mine = ConversationSession.objects.create(user=cook, bot_name="kitchen")
    elsewhere = ConversationSession.objects.create(user=cook, bot_name="other-bot")
    for session_id in (str(theirs.id), str(elsewhere.id), "not-a-uuid"):
        assert call(client, "note", {"text": "x"}, session_id=session_id).status_code == 404
    assert kitchen.table("Pantry").objects.count() == 1
    done = call(client, "note", {"text": "x"}, session_id=str(mine.id))
    assert done.status_code == 200
    assert done.json() == {"result": {"seen_from": mine.id.hex}}


@pytest.mark.django_db(transaction=True)
def test_an_action_that_needs_approval_takes_a_signed_round_trip(client, kitchen, cook, monkeypatch):
    session = ConversationSession.objects.create(user=cook, bot_name="kitchen")
    args = {"item": "flour", "qty": 2}
    where = {"session_id": str(session.id), "page": "pages/board.jhtml"}

    first = call(client, "restock", args, **where).json()
    assert first["needs_approval"] is True
    assert first["preview"] == "Order 2 x flour"
    assert kitchen.table("Pantry").objects.get(name="flour").on_order == 0
    assert PageActionCall.objects.count() == 0  # asking isn't an action

    # The token is for these arguments, from this user.
    wrong = call(client, "restock", {**args, "qty": 99}, approval=first["approval"], **where)
    assert wrong.status_code == 403
    assert call(client, "restock", args, approval="made-up", **where).status_code == 403
    monkeypatch.setattr(page_actions, "APPROVAL_MAX_AGE", -1)
    expired = call(client, "restock", args, approval=first["approval"], **where)
    assert (expired.status_code, "expired" in expired.json()["detail"]) == (400, True)
    monkeypatch.undo()
    assert kitchen.table("Pantry").objects.get(name="flour").on_order == 0

    done = call(client, "restock", args, approval=first["approval"], **where)
    assert done.status_code == 200
    assert done.json()["result"] == {
        "message": "Ordered 2 flour",
        "reload": True,
        "by": "cook",
        "page": "pages/board.jhtml",
    }
    assert kitchen.table("Pantry").objects.get(name="flour").on_order == 2

    [recorded] = PageActionCall.objects.all()
    assert (recorded.bot, recorded.action, recorded.user, recorded.session) == ("kitchen", "restock", cook, session)
    assert (recorded.page, recorded.args, recorded.approved) == ("pages/board.jhtml", args, True)
    assert recorded.result["message"] == "Ordered 2 flour"
    assert recorded.error == "" and recorded.ok
    assert recorded.duration_ms >= 0


@pytest.mark.django_db(transaction=True)
def test_failures_are_reported_and_recorded_without_leaking_internals(client, kitchen, cook):
    refused = call(client, "refuse", {"why": "Not on a Sunday"})
    assert (refused.status_code, refused.json()["detail"]) == (400, "Not on a Sunday")
    broke = call(client, "explode")
    assert (broke.status_code, broke.json()["detail"]) == (500, "The action failed")
    assert [(c.action, c.error, c.result) for c in PageActionCall.objects.order_by("created_at")] == [
        ("refuse", "Not on a Sunday", None),
        ("explode", "The action failed", None),
    ]


@pytest.mark.django_db(transaction=True)
def test_an_action_over_its_time_budget_answers_504_and_is_recorded_when_it_finishes(
    client,
    kitchen,
    cook,
    monkeypatch,
):
    monkeypatch.setattr(page_actions, "TIMEOUT_SECONDS", 0.1)
    response = call(client, "slow")
    assert response.status_code == 504
    assert "ctx.tasks" in response.json()["detail"]
    for _ in range(50):
        if PageActionCall.objects.filter(action="slow").exists():
            break
        time.sleep(0.1)
    assert PageActionCall.objects.get(action="slow").result == {"done": True}


@pytest.mark.django_db(transaction=True)
def test_the_bot_sees_page_actions_since_its_last_reply(client, kitchen, cook):
    session = ConversationSession.objects.create(user=cook, bot_name="kitchen")
    where = {"session_id": str(session.id)}
    call(client, "note", {"text": "salt"}, **where)
    call(client, "refuse", {"why": "Nope"}, **where)
    call(client, "note", {"text": "unrelated chat"})  # no session: the bot has nothing to show for it

    def titles_and_text(message="hi"):
        builder = kitchen.context_builder(session, message)
        sections = builder.build().sections if builder else []
        return {s.title: s.body for s in sections}

    shown = titles_and_text()[page_actions.CONTEXT_TITLE].splitlines()
    assert shown == [
        f'- cook ran note({{"text": "salt"}}): ok: {{"seen_from": "{session.id.hex}"}}',
        '- cook ran refuse({"why": "Nope"}): failed: Nope',
    ]

    # Once the bot has replied, they're history.
    SessionMessage.objects.create(session=session, role="assistant", sequence=1)
    assert page_actions.CONTEXT_TITLE not in titles_and_text()

    # And only the last 20 are kept.
    for n in range(25):
        call(client, "note", {"text": f"n{n}"}, **where)
    lines = titles_and_text()[page_actions.CONTEXT_TITLE].splitlines()
    assert len(lines) == 20
    assert '"n24"' in lines[-1] and '"n5"' in lines[0]
