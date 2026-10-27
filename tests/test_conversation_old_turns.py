"""Tests for shortening older turns' long messages in what each model call sends."""

from __future__ import annotations

import copy
import json
from types import SimpleNamespace

from django_ergo.conversation.old_turns import REPEAT_NOTE
from django_ergo.conversation.old_turns import trim_old_turns

REPORT = "Worker report. " + "x" * 3000
BRIEF = "Please do the thing. " + "y" * 3000
REPLY = "The PR is merged and the checks passed. " * 10


def relayed(text):
    return {"role": "user", "content": [{"type": "text", "text": text}]}


def claude_turn(user_text, *, brief=BRIEF, reply=REPLY, n=0):
    return [
        relayed(user_text),
        {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": f"send{n}",
                    "name": "ergo_thread_send",
                    "input": {"thread": "abc", "message": brief},
                }
            ],
        },
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": f"send{n}", "content": "sent"}
            ],
        },
        {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": f"reply{n}",
                    "name": "send_reply",
                    "input": {"text": reply},
                }
            ],
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": f"reply{n}",
                    "content": "Output accepted.",
                }
            ],
        },
        {"role": "assistant", "content": [{"type": "text", "text": reply}]},
    ]


def from_thread(message):
    return SimpleNamespace(
        provenance={
            "kind": "report",
            "origin": {"label": "devbox · Build it", "session_id": "t-1"},
        }
    )


def rows_for(messages, *, relayed_indexes):
    return {id(messages[i]): from_thread(messages[i]) for i in relayed_indexes}


def test_old_relayed_messages_and_briefs_are_cut_newest_turns_kept():
    history = [m for n in range(4) for m in claude_turn(REPORT, n=n)]
    before = copy.deepcopy(history)
    rows = rows_for(history, relayed_indexes=[0, 6, 12, 18])
    stats = {}
    sent = trim_old_turns(history, rows, keep_turns=3, max_chars=100, stats=stats)

    assert history == before  # the stored copy is untouched
    first = sent[0]["content"][0]["text"]
    assert first.startswith("Worker report.")
    assert len(first) < 300
    assert "the full message is in devbox · Build it (thread t-1)" in first
    brief = sent[1]["content"][0]["input"]["message"]
    assert brief.startswith("Please do the thing.")
    assert "in this chat's history" in brief
    assert sent[1]["content"][0]["id"] == "send0"
    # The newest three turns go as stored, apart from the repeated reply.
    assert sent[6]["content"][0]["text"] == REPORT
    assert sent[7]["content"][0]["input"]["message"] == BRIEF
    assert stats["trimmed_messages"] == 2 + 4  # turn 0's report and brief, four replies


def test_the_users_own_long_messages_stay():
    history = [m for n in range(4) for m in claude_turn(REPORT, n=n)]
    sent = trim_old_turns(history, {}, keep_turns=1, max_chars=100)
    assert sent[0]["content"][0]["text"] == REPORT


def test_a_reply_repeated_as_the_final_text_is_sent_once():
    history = claude_turn("hi")
    sent = trim_old_turns(history, {}, keep_turns=3, max_chars=100_000)
    assert sent[3]["content"][0]["input"] == {"text": REPEAT_NOTE}
    assert sent[5]["content"][0]["text"] == REPLY
    assert sent[1]["content"][0]["input"]["message"] == BRIEF


def test_a_turn_still_running_keeps_its_reply_call():
    history = claude_turn("hi")[:5]  # no final text yet
    assert trim_old_turns(history, {}, keep_turns=3, max_chars=100_000) is history


def test_openai_messages():
    history = []
    for n in range(2):
        history += [
            {"role": "user", "content": REPORT},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": f"c{n}",
                        "type": "function",
                        "function": {
                            "name": "ergo_thread_send",
                            "arguments": json.dumps({"message": BRIEF}),
                        },
                    }
                ],
            },
            {"role": "tool", "tool_call_id": f"c{n}", "content": "sent"},
            {"role": "assistant", "content": "Sent."},
        ]
    rows = rows_for(history, relayed_indexes=[0, 4])
    sent = trim_old_turns(history, rows, keep_turns=1, max_chars=100)
    assert "the full message is in devbox" in sent[0]["content"]
    arguments = json.loads(sent[1]["tool_calls"][0]["function"]["arguments"])
    assert "in this chat's history" in arguments["message"]
    assert sent[4]["content"] == REPORT


def test_none_turns_cutting_off(settings):
    settings.DJANGO_ERGO = {"OLD_TURNS_IN_FULL": None}
    history = [m for n in range(4) for m in claude_turn(REPORT, reply="short", n=n)]
    rows = rows_for(history, relayed_indexes=[0])
    assert trim_old_turns(history, rows) is history
