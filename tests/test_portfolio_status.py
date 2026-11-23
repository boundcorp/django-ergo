"""ergo_portfolio_status: the one-call stored portfolio snapshot (bots.snapshot)."""

from __future__ import annotations

import json
import time
from datetime import timedelta
from types import SimpleNamespace

import pytest
from asgiref.sync import async_to_sync
from django.contrib.auth import get_user_model
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from django_ergo.bots import orchestrator
from django_ergo.bots import snapshot
from django_ergo.bots.registry import BotRegistry
from django_ergo.bots.tools import ToolContext
from django_ergo.conversation.links import GITHUB_PR
from django_ergo.conversation.models import ConversationAttachment
from django_ergo.conversation.models import ConversationCompaction
from django_ergo.conversation.models import ConversationSession
from django_ergo.conversation.models import PrivateExport
from django_ergo.conversation.models import StructuredCall
from django_ergo.conversation.models import ThreadMessage
from django_ergo.conversation.models import Worker
from tests.test_bots import write_bot
from tests.test_conversation_structured import claude_engine

pytestmark = pytest.mark.django_db(transaction=True)
User = get_user_model()


def make_world(tmp_path, *, hub_extra=""):
    """boundcorp (parent, with attachments) over design and kitchen (siblings)
    and vault (private)."""
    parent = write_bot(
        tmp_path,
        f"name: boundcorp\nplugins: [{{name: attachments}}]\n{hub_extra}",
        name="boundcorp",
    )
    for name, extra in (
        ("design", "plugins: [{name: attachments}]\n"),
        ("kitchen", ""),
        ("vault", "private: true\nplugins: [{name: attachments}]\n"),
    ):
        write_bot(parent, f"name: {name}\n{extra}", name=name)
    return BotRegistry.discover(parent, engine_factory=claude_engine)


@pytest.fixture
def world(tmp_path):
    registry = make_world(tmp_path)
    user = User.objects.create_user(username="snap-owner")
    other = User.objects.create_user(username="snap-other")
    return SimpleNamespace(registry=registry, user=user, other=other)


def ctx_for(world, bot_name, user=None, session=None):
    bot = world.registry.get(bot_name)
    user = user or world.user
    session = session or async_to_sync(bot.root_session)(user)
    return ToolContext(bot=bot, session=session, user=user)


def thread(world, bot_name, title, user=None, **fields):
    bot = world.registry.get(bot_name)
    session = async_to_sync(bot.create_session)(user or world.user, title=title)
    if fields:
        ConversationSession.objects.filter(pk=session.pk).update(**fields)
        session.refresh_from_db()
    return session


def titles(rows):
    return {r["title"] for r in rows}


def test_scope_follows_the_callers_bot_and_user(world):
    main = ctx_for(world, "boundcorp")
    thread(world, "design", "Logo")
    thread(world, "kitchen", "Menu")
    thread(world, "vault", "Private plans")
    thread(world, "design", "Someone else's", user=world.other)
    out = snapshot.portfolio_status(main)
    assert out["scope"]["bots"] == ["boundcorp", "design", "kitchen"]
    assert {"design · Logo", "kitchen · Menu"} <= titles(out["threads"])
    text = json.dumps(out)
    assert "Private plans" not in text and "vault" not in text
    assert "Someone else's" not in text

    # A sub-bot sees itself and its parent, not its sibling or the private bot.
    design = snapshot.portfolio_status(ctx_for(world, "design"))
    assert design["scope"]["bots"] == ["design", "boundcorp"]
    assert "kitchen · Menu" not in titles(design["threads"])
    assert "Private plans" not in json.dumps(design)

    # Another user sees only their own chats.
    theirs = snapshot.portfolio_status(ctx_for(world, "boundcorp", user=world.other))
    assert titles(theirs["threads"]) == {"boundcorp · Main", "design · Someone else's"}


def test_a_bot_filter_out_of_scope_looks_like_a_missing_bot(world):
    design = ctx_for(world, "design")
    errors = []
    for name in ("kitchen", "vault", "nonexistent"):
        with pytest.raises(ValueError, match="No bot") as caught:
            snapshot.portfolio_status(design, bot=name)
        errors.append(str(caught.value).replace(name, "X"))
    assert len(set(errors)) == 1
    only = snapshot.portfolio_status(ctx_for(world, "boundcorp"), bot="design")
    assert only["scope"]["bots"] == ["design"]


def test_main_and_named_chats_are_listed_and_never_changed(world):
    kitchen = world.registry.get("kitchen")
    main = ctx_for(world, "boundcorp")
    named = ConversationSession.objects.create(
        user=world.user,
        bot_name="kitchen",
        status="completed",  # a top-level chat stays listed whatever its status
        metadata={"bot_role": "chat", "chat": "reports", "title": "Reports"},
    )
    before = list(
        ConversationSession.objects.order_by("id").values_list(
            "id", "status", "metadata", "updated_at"
        )
    )
    out = snapshot.portfolio_status(main)
    kinds = {r["session_id"]: r["kind"] for r in out["threads"]}
    assert kinds[str(named.id)] == "named_chat"
    assert kinds[str(main.session.id)] == "main"
    assert out["threads"][0] or kitchen
    assert [r["session_id"] for r in out["resolved"]] == []
    after = list(
        ConversationSession.objects.order_by("id").values_list(
            "id", "status", "metadata", "updated_at"
        )
    )
    assert before == after


def test_resolved_window_includes_the_boundary_and_excludes_older(world, monkeypatch):
    now = timezone.now()
    ctx = ctx_for(world, "boundcorp")
    cutoff = now - timedelta(hours=24)
    inside = thread(world, "design", "inside", status="completed")
    edge = thread(world, "design", "edge", status="completed")
    outside = thread(world, "design", "outside", status="completed")
    for session, when in (
        (inside, cutoff + timedelta(seconds=1)),
        (edge, cutoff),
        (outside, cutoff - timedelta(seconds=1)),
    ):
        ConversationSession.objects.filter(pk=session.pk).update(
            updated_at=when,
            metadata={
                **session.metadata,
                "archived_at": when.isoformat(),
                "resolved_by": "boundcorp · Main",
                "resolved_summary": f"{session.metadata['title']} shipped",
            },
        )
    monkeypatch.setattr(snapshot.timezone, "now", lambda: now)
    out = snapshot.portfolio_status(ctx, lookback_hours=24)
    assert [r["title"] for r in out["resolved"]] == ["design · inside", "design · edge"]
    assert out["resolved"][0]["resolved_summary"] == "inside shipped"
    assert out["coverage"]["resolved"]["total"] == 2
    assert "design · inside" not in titles(out["threads"])  # not also "active"
    # A resolved thread of a sibling bot is not visible to design.
    sibling = thread(world, "kitchen", "sibling done", status="completed")
    seen = snapshot.portfolio_status(ctx_for(world, "design"))["resolved"]
    assert "kitchen · sibling done" not in titles(seen)
    assert "kitchen · sibling done" in titles(
        snapshot.portfolio_status(ctx)["resolved"]
    )
    assert sibling


def test_recorded_state_and_no_inferred_blockers(world):
    ctx = ctx_for(world, "boundcorp")
    waiting = thread(world, "design", "needs approval")
    asking = thread(world, "design", "asks")
    quiet = thread(world, "design", "quiet")
    StructuredCall.objects.create(
        session=waiting, kind="chat_reply", status="awaiting_approval"
    )
    StructuredCall.objects.create(
        session=asking,
        kind="chat_reply",
        status="completed",
        response={"type": "question", "text": "Navy or teal?"},
    )
    ThreadMessage.objects.create(
        sender_session=ctx.session,
        recipient_session=waiting,
        text="do it",
        status="waiting",
    )
    ConversationCompaction.objects.create(
        session=quiet,
        mode="context_size",
        from_sequence=0,
        upto_sequence=4,
        summary="Chose the palette.",
    )
    ConversationSession.objects.filter(pk=quiet.pk).update(
        updated_at=timezone.now() - timedelta(hours=5)
    )
    rows = {r["title"]: r for r in snapshot.portfolio_status(ctx)["threads"]}
    approval = rows["design · needs approval"]
    assert approval["state"] == "waiting_for_approval"
    assert approval["recorded"]["pending_approval"] is True
    assert rows["boundcorp · Main"]["recorded"]["waiting_on_approval_of"] == [
        "design · needs approval"
    ]
    assert approval["recorded"]["open_requests_in"] == 1
    question = rows["design · asks"]
    assert question["recorded"]["asks_user"] is True
    assert question["latest_reply"]["text"] == "Navy or teal?"
    assert question["latest_reply"]["type"] == "question"
    old = rows["design · quiet"]
    assert old["summary"] == "Chose the palette."
    assert old["recorded"]["asks_user"] is False
    assert old["state"] == "idle" and "ready_to_resolve" not in json.dumps(old)
    assert rows["boundcorp · Main"]["you_are_here"] is True


def test_workers_show_owner_status_and_activity_for_in_scope_chats_only(world):
    ctx = ctx_for(world, "boundcorp")
    mine = thread(world, "design", "Build")
    hidden = thread(world, "vault", "Secret build")
    kitchen_other = thread(world, "design", "x", user=world.other)
    epoch = (timezone.now() - timedelta(minutes=3)).timestamp()
    running = Worker.objects.create(
        session=mine,
        bot_name="design",
        title="Agent: implement logo",
        function="orca:agent",
        status="running",
        progress="editing files",
        args={"token": "SECRET-ARG"},
        state={"activity": {"at": epoch}},
        error="boom SECRET-ERROR",
    )
    old = Worker.objects.create(
        session=mine,
        bot_name="design",
        title="old",
        function="task:x",
        status="completed",
    )
    Worker.objects.filter(pk=old.pk).update(
        updated_at=timezone.now() - timedelta(days=10)
    )
    for session in (hidden, kitchen_other):
        Worker.objects.create(
            session=session,
            bot_name=session.bot_name,
            title="hidden",
            function="task:y",
            status="running",
        )
    out = snapshot.portfolio_status(ctx)
    [row] = out["workers"]
    assert row["id"] == str(running.id)
    assert row["owner"]["title"] == "design · Build"
    assert (row["status"], row["progress"], row["kind"]) == (
        "running",
        "editing files",
        "orca",
    )
    assert row["has_error"] is True
    assert row["last_activity"]
    text = json.dumps(out)
    assert "SECRET" not in text and "hidden" not in text
    thread_row = next(r for r in out["threads"] if r["title"] == "design · Build")
    assert thread_row["recorded"]["workers_running"] == 1


def test_pull_requests_say_unknown_or_not_stored(world):
    ctx = ctx_for(world, "boundcorp")
    mine = thread(world, "design", "Build")
    fresh = "2026-11-23T10:00:00+00:00"

    def pr(session, number, **meta):
        return ConversationAttachment.objects.create(
            session=session,
            source="bot",
            kind="document",
            media_type="text/uri-list",
            url=f"https://github.com/boundcorp/django-ergo/pull/{number}",
            filename=f"django-ergo#{number}",
            metadata={
                "link": GITHUB_PR,
                "repo": "boundcorp/django-ergo",
                "number": number,
                **meta,
            },
        )

    pr(mine, 1, state="open", checks="failing", checked_at=fresh, title="Fix it")
    pr(ctx.session, 1, state="open", checks="failing", checked_at=fresh)  # same PR
    pr(mine, 2)  # recorded, never refreshed
    pr(thread(world, "vault", "v"), 3, state="open")
    old = pr(mine, 4, state="merged", checks="passing", checked_at=fresh)
    ConversationAttachment.objects.filter(pk=old.pk).update(
        updated_at=timezone.now() - timedelta(days=30)
    )
    out = snapshot.portfolio_status(ctx)
    by_number = {p["number"]: p for p in out["pull_requests"]}
    assert set(by_number) == {1, 2}
    first, second = by_number[1], by_number[2]
    assert (first["state"], first["checks"], first["checks_stored_at"]) == (
        "open",
        "failing",
        fresh,
    )
    assert len(first["chats"]) == 2  # both in-scope chats that recorded it
    assert first["review"] == second["review"] == "not stored"
    assert (second["state"], second["checks"], second["checks_stored_at"]) == (
        "unknown",
        "not stored",
        "not stored",
    )
    freshness = out["coverage"]["pull_requests"]
    assert freshness["never_refreshed"] == 1
    assert freshness["freshness"]["newest"] == fresh
    assert out["snapshot"]["mode"] == "stored_snapshot"
    assert out["snapshot"]["live_refresh"] == "not performed"
    assert "vault" not in json.dumps(out)


def test_attachment_visibility_and_links(world, tmp_path, settings):
    settings.MEDIA_ROOT = tmp_path / "media"
    from django_ergo.conversation.attachments import save_session_file

    ctx = ctx_for(world, "boundcorp")
    design = thread(world, "design", "Design chat")
    vault = thread(world, "vault", "Vault chat")
    mine = save_session_file(
        ctx.session, "plan.md", b"plan", media_type="text/markdown"
    )
    shared = save_session_file(
        design, "logo.svg", b"<svg/>", media_type="image/svg+xml"
    )
    save_session_file(vault, "vault.txt", b"private", media_type="text/plain")
    call = StructuredCall.objects.create(session=vault, kind="chat_reply")
    receipt = PrivateExport.objects.create(
        source=vault,
        destination=design,
        call=call,
        authorization_id="a",
        actor_id="u",
        tool_use_id="t",
        manifest_sha256="0" * 64,
    )
    copy = save_session_file(design, "export.txt", b"exported", media_type="text/plain")
    ConversationAttachment.objects.filter(pk=copy.pk).update(private_export=receipt)
    gone = save_session_file(design, "gone.txt", b"x", media_type="text/plain")
    ConversationAttachment.objects.filter(pk=gone.pk).update(archived_at=timezone.now())

    out = snapshot.portfolio_status(ctx)
    files = {a["filename"]: a for a in out["attachments"]}
    # Another chat's private-export copy, the private bot's files and archived
    # files are absent; the rest carry links.
    assert set(files) == {"plan.md", "logo.svg"}
    assert files["logo.svg"]["link"] == f"/api/attachments/{shared.id}/download"
    assert files["plan.md"]["link"].endswith(f"{mine.id}/download")
    assert out["coverage"]["attachments"]["total"] == 2
    assert "export.txt" not in json.dumps(out) and "vault" not in json.dumps(out)

    # In the chat the copy was exported to, the copy is visible.
    in_design = snapshot.portfolio_status(ctx_for(world, "design", session=design))
    assert "export.txt" in {a["filename"] for a in in_design["attachments"]}

    # A bot whose attachments plugin keeps to its own session sees only its own files.
    plugin = ctx.bot.plugin("attachments")
    plugin.other_sessions = False
    own = snapshot.portfolio_status(ctx)
    assert {a["filename"] for a in own["attachments"]} == {"plan.md"}
    # The kitchen bot has no attachments plugin: it never lists other chats' files.
    kitchen = ctx_for(world, "kitchen")
    save_session_file(kitchen.session, "k.txt", b"k", media_type="text/plain")
    assert {
        a["filename"] for a in snapshot.portfolio_status(kitchen)["attachments"]
    } == {"k.txt"}


def test_a_failing_source_is_reported_and_the_rest_still_return(world, monkeypatch):
    ctx = ctx_for(world, "boundcorp")
    thread(world, "design", "Logo")

    def broken(*args, **kwargs):
        message = "db exploded: password=hunter2"
        raise RuntimeError(message)

    monkeypatch.setitem(snapshot.READERS, "workers", broken)
    out = snapshot.portfolio_status(ctx)
    assert out["partial"] is True
    assert out["errors"] == {"workers": "workers unavailable (RuntimeError)"}
    assert out["coverage"]["workers"]["status"] == "error"
    assert out["coverage"]["threads"]["status"] == "ok"
    assert "design · Logo" in titles(out["threads"])
    assert "hunter2" not in json.dumps(out)

    only = snapshot.portfolio_status(ctx, sources=["threads"])
    assert set(only["coverage"]) == {"threads"} and not only["partial"]


def test_pagination_and_truncation_markers(world):
    ctx = ctx_for(world, "boundcorp")
    for i in range(5):
        thread(world, "design", f"t{i}")
    first = snapshot.portfolio_status(ctx, limit=2, sources=["threads"])
    info = first["coverage"]["threads"]
    assert (info["returned"], info["total"], info["truncated"]) == (2, 6, True)
    assert info["next_offset"] == 2
    seen = {r["session_id"] for r in first["threads"]}
    last = snapshot.portfolio_status(ctx, limit=2, offset=4, sources=["threads"])
    assert last["coverage"]["threads"]["truncated"] is False
    assert not seen & {r["session_id"] for r in last["threads"]}
    assert first["coverage"]["threads"]["freshness"]["read_at"]


def test_text_fields_are_clipped(world):
    ctx = ctx_for(world, "boundcorp")
    long = thread(world, "design", "long")
    StructuredCall.objects.create(
        session=long,
        kind="chat_reply",
        status="completed",
        response={"type": "message", "text": "word " * 2000},
    )
    row = next(
        r
        for r in snapshot.portfolio_status(ctx)["threads"]
        if r["title"] == "design · long"
    )
    assert len(row["latest_reply"]["text"]) <= snapshot.EVIDENCE_CHARS


def test_the_tool_is_one_call_for_orchestrating_bots_only(world):
    ctx = ctx_for(world, "boundcorp")
    thread(world, "design", "Logo")
    toolkit = orchestrator.orchestrator_toolkit(ctx)
    assert "ergo_portfolio_status" in toolkit.tools
    with CaptureQueriesContext(connection) as queries:
        raw = toolkit.execute_tool("ergo_portfolio_status", {"lookback_hours": 48})
    out = json.loads(raw)
    assert out["snapshot"]["lookback_hours"] == 48
    assert "design · Logo" in titles(out["threads"])
    assert len(queries) < 40
    with pytest.raises(orchestrator.ToolError):
        toolkit.execute_tool("ergo_portfolio_status", {"bot": "vault"})
    assert "ergo_portfolio_status" not in orchestrator.upward_toolkit(ctx).tools


def test_query_count_is_bounded_and_does_not_grow_with_chats(world):
    ctx = ctx_for(world, "boundcorp")

    def run():
        with CaptureQueriesContext(connection) as queries:
            snapshot.portfolio_status(ctx)
        return len(queries)

    def populate(count):
        for i in range(count):
            session = thread(world, "design" if i % 2 else "kitchen", f"c{i}")
            StructuredCall.objects.create(
                session=session,
                kind="chat_reply",
                status="completed",
                response={"type": "message", "text": f"reply {i}"},
            )
            Worker.objects.create(
                session=session,
                bot_name=session.bot_name,
                title=f"w{i}",
                function="task:a",
                status="running",
            )

    populate(3)
    small = run()
    populate(40)
    large = run()
    assert small == large <= 30


def test_measured_on_a_representative_portfolio(world, capsys):
    """Scale: 102 chats (60 open threads and 40 resolved across 2 bots, plus 2
    mains), 100 stored replies, 60 running workers, 100 PRs, 100 files. Prints the numbers that
    the docs quote; asserts only loose ceilings."""
    ctx = ctx_for(world, "boundcorp")
    now = timezone.now()
    for i in range(100):
        name = ("design", "kitchen")[i % 2]
        session = thread(
            world, name, f"work {i}", status="completed" if i % 5 < 2 else ""
        )
        StructuredCall.objects.create(
            session=session,
            kind="chat_reply",
            status="completed",
            response={"type": "message", "text": "Progress update. " * 60},
        )
        if i % 5 >= 2:
            Worker.objects.create(
                session=session,
                bot_name=name,
                title=f"agent {i}",
                function="orca:agent",
                status="running",
                progress="working " * 10,
            )
        ConversationAttachment.objects.create(
            session=session,
            source="bot",
            kind="document",
            media_type="text/uri-list",
            url=f"https://github.com/boundcorp/django-ergo/pull/{i}",
            filename=f"django-ergo#{i}",
            metadata={
                "link": GITHUB_PR,
                "repo": "boundcorp/django-ergo",
                "number": i,
                "state": "open",
                "checks": "passing",
                "checked_at": now.isoformat(),
                "title": "A reasonably long pull request title for the fixture",
            },
        )
        ConversationAttachment.objects.create(
            session=session,
            source="bot",
            kind="document",
            media_type="text/plain",
            filename=f"f{i}.txt",
            size=10,
        )
    snapshot.portfolio_status(ctx)  # warm
    started = time.perf_counter()
    with CaptureQueriesContext(connection) as queries:
        out = snapshot.portfolio_status(ctx)
    elapsed = (time.perf_counter() - started) * 1000
    size = len(json.dumps(out))
    with capsys.disabled():
        print(
            f"\nMEASURED chats=101 queries={len(queries)} latency_ms={elapsed:.0f} "
            f"json_bytes={size} threads={len(out['threads'])} resolved={len(out['resolved'])} "
            f"workers={len(out['workers'])} prs={len(out['pull_requests'])} "
            f"files={len(out['attachments'])}"
        )
    assert len(queries) <= 30 and size < 120_000
