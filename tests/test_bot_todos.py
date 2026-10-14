from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model

from django_ergo.bots.todos import get_todos
from django_ergo.bots.todos import render
from tests.test_bots import make_bot
from tests.test_bots import say
from tests.test_conversation_structured import claude_tool

User = get_user_model()


@pytest.mark.django_db(transaction=True)
async def test_every_chat_keeps_a_todo_list_the_model_sees(tmp_path):
    user = await User.objects.acreate(username="todo")
    steps = [
        {"content": "Read the code", "status": "in_progress"},
        {"content": "Write the tools", "status": "pending"},
    ]
    bot, engine = make_bot(
        tmp_path,
        claude_tool("ergo_todo_write", {"todos": steps}),
        claude_tool("ergo_todo_update", {"item": 1, "status": "completed"}),
        say("Started."),
        say("Next."),
    )
    main = await bot.main_session(user)

    await bot.ask(main, "Build the todo tools")

    first = engine._client.calls[0]
    assert {"ergo_todo_write", "ergo_todo_update"} <= {
        t["name"] for t in first["tools"]
    }
    await main.arefresh_from_db()
    assert get_todos(main) == [
        {"content": "Read the code", "status": "completed"},
        {"content": "Write the tools", "status": "pending"},
    ]

    await bot.ask(main, "Go on")

    context = engine._client.calls[-1]["messages"][-1]["content"][0]["text"]
    assert "1 of 2 done:\n1. [x] Read the code\n2. [ ] Write the tools" in context


@pytest.mark.django_db(transaction=True)
def test_todo_tools_check_their_input_and_clear(tmp_path):
    from django_ergo.bots.tools import ToolContext

    bot, _ = make_bot(tmp_path)
    user = User.objects.create(username="todo2")
    session = bot.sessions().create(user=user, bot_name=bot.name, metadata={"x": 1})
    skill = next(d for d in bot.skill_defs if d.name == "todos")
    assert skill.always
    kit = skill.toolkits(ToolContext(bot=bot, session=session, user=user))[0]

    with pytest.raises(ValueError, match="list is empty"):
        kit.execute_tool("ergo_todo_update", {"item": 1, "status": "completed"})
    with pytest.raises(ValueError, match="status must be one of"):
        kit.execute_tool(
            "ergo_todo_write", {"todos": [{"content": "a", "status": "done"}]}
        )
    with pytest.raises(ValueError, match="needs content"):
        kit.execute_tool("ergo_todo_write", {"todos": [{"content": " "}]})

    out = kit.execute_tool(
        "ergo_todo_write", {"todos": ["Plain string", {"content": "b"}]}
    )
    assert out == "0 of 2 done:\n1. [ ] Plain string\n2. [ ] b"
    with pytest.raises(ValueError, match="No item 3"):
        kit.execute_tool("ergo_todo_update", {"item": 3, "status": "completed"})
    out = kit.execute_tool(
        "ergo_todo_update", {"item": 2, "status": "in_progress", "content": "B"}
    )
    assert out.endswith("2. [~] B")

    session.refresh_from_db()
    assert session.metadata["x"] == 1  # the rest of the metadata stays
    assert kit.execute_tool("ergo_todo_write", {"todos": []}) == render([])
    session.refresh_from_db()
    assert "todos" not in session.metadata


def test_todos_can_be_turned_off(tmp_path):
    bot, _ = make_bot(tmp_path, yaml_text="name: kitchen\ntodos: false\ntools: []\n")
    assert bot.definition.todos is False
    assert "todos" not in {d.name for d in bot.skill_defs}
