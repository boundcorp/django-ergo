"""The per-turn context cost report: structure, measured numbers and labels."""

from __future__ import annotations

import json
from io import StringIO

import pytest
from django.contrib.auth import get_user_model
from django.core.management import call_command

from django_ergo.bots.cost import LEGEND
from django_ergo.bots.cost import render_text
from django_ergo.bots.cost import turn_cost
from django_ergo.conversation.models import ConversationSession
from django_ergo.conversation.models import MessageBlock
from django_ergo.conversation.models import SessionMessage
from tests.test_bots import make_bot

User = get_user_model()


def seed_history(session):
    def message(sequence, role, **fields):
        return SessionMessage.objects.create(
            session=session, role=role, sequence=sequence, **fields
        )

    def block(msg, sequence, block_type, **fields):
        return MessageBlock.objects.create(
            message=msg, block_type=block_type, sequence=sequence, **fields
        )

    block(message(1, "user"), 0, "text", text="x" * 40)
    reply = message(
        2,
        "assistant",
        input_tokens=1234,
        cache_read_input_tokens=1000,
        model_name="claude-test",
    )
    block(reply, 0, "text", text="y" * 10)
    block(
        reply,
        1,
        "tool_use",
        tool_use_id="t1",
        tool_name="search",
        tool_input={"q": "a"},
    )
    result = message(3, "user")
    block(result, 0, "tool_result", tool_result_for="t1", tool_result_content="z" * 500)


@pytest.mark.django_db
def test_turn_cost_rows_are_labelled_and_measured(tmp_path):
    bot, _ = make_bot(tmp_path)
    session = ConversationSession.objects.create(
        user=User.objects.create(username="cost-lee"),
        bot_name="kitchen",
        system_prompt="You run the kitchen.",
        metadata={"skills": {"pantry": 0}},
    )
    seed_history(session)

    report = turn_cost(bot, session, "hello")

    assert report["legend"] == LEGEND
    system = {r["name"]: r for r in report["system"]}
    assert system["instructions"]["chars"] == len("You run the kitchen.")
    # Estimates are chars/4 rounded up, labelled as est_tokens, never as tokens.
    assert (
        system["instructions"]["est_tokens"] == (len("You run the kitchen.") + 3) // 4
    )
    assert all(
        "tokens" not in key.replace("est_tokens", "")
        for r in report["system"]
        for key in r
    )
    assert report["tools"][0]["name"] == "skill loader tools"
    assert report["tools"][0]["chars"] > 0
    assert any(r["name"] == "Skills" for r in report["context"])
    kitchen = {s["name"]: s for s in report["skills_on_load"]}
    assert kitchen["pantry"]["loaded"] is True
    assert "pantry" in [r["name"] for r in report["tools"]]
    assert kitchen["pantry"]["loaded"] and not kitchen["orchestration"]["loaded"]
    assert kitchen["pantry"]["tool_count"] >= 1
    assert kitchen["pantry"]["tool_schemas"]["chars"] > 0

    history = report["history"]
    assert history["messages"] == 3
    assert history["by_block"]["user_text"]["chars"] == 40
    assert history["by_block"]["assistant_text"]["chars"] == 10
    assert history["by_block"]["tool_result"]["chars"] == len(json.dumps("z" * 500))
    assert history["tool_results_by_tool"][0]["tool"] == "search"
    reported = history["provider_reported_last_request"]
    assert reported["input_tokens"] == 1234
    assert reported["basis"] == "provider_reported"

    totals = report["totals"]
    parts = [*report["system"], *report["tools"], *report["context"]]
    expected = sum(r["chars"] for r in parts) + sum(
        v["chars"] for v in history["by_block"].values()
    )
    assert totals["chars"] == expected
    assert totals["est_tokens"] == (expected + 3) // 4
    assert "estimate" in totals["basis"]
    assert "images and attachment blocks" in totals["excludes"]
    json.dumps(report)  # plain data


@pytest.mark.django_db
def test_text_report_names_proxy_and_estimate_and_reported(tmp_path):
    bot, _ = make_bot(tmp_path)
    session = ConversationSession.objects.create(
        user=User.objects.create(username="cost-text"), bot_name="kitchen"
    )
    seed_history(session)
    text = render_text(turn_cost(bot, session))
    assert "exact characters (proxy)" in text
    assert "chars/4 estimate" in text
    assert "reported: last request had 1234 input tokens" in text
    assert "(estimate)" in text.splitlines()[-1]


@pytest.mark.django_db
def test_report_is_read_only(tmp_path):
    bot, engine = make_bot(tmp_path)
    session = ConversationSession.objects.create(
        user=User.objects.create(username="cost-ro"), bot_name="kitchen"
    )
    seed_history(session)
    before = (SessionMessage.objects.count(), MessageBlock.objects.count())
    turn_cost(bot, session)
    assert engine._client.calls == []  # no model call
    assert before == (SessionMessage.objects.count(), MessageBlock.objects.count())


@pytest.mark.django_db
def test_command_uses_a_throwaway_session_and_rolls_back(tmp_path):
    from tests.test_bots import KITCHEN_YAML
    from tests.test_bots import write_bot

    folder = write_bot(tmp_path, KITCHEN_YAML)
    before = (User.objects.count(), ConversationSession.objects.count())
    out = StringIO()
    call_command("ergo_bot_cost", str(folder), "--json", stdout=out)
    data = json.loads(out.getvalue())
    assert data["bot"] == "kitchen" and data["legend"]["chars"]
    assert before == (User.objects.count(), ConversationSession.objects.count())
    text = StringIO()
    call_command("ergo_bot_cost", str(folder), stdout=text)
    assert "Total measured" in text.getvalue()
