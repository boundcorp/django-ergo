"""The Ergo client skill's ``ergonaut-remote`` command against a live server, with an API key."""

import importlib.util
import json
import textwrap

import django_ergo.bots
import pytest
from django.contrib.auth import get_user_model
from django_ergo.bots import webhooks

from ergonaut.apps.bots.loading import load_registry
from ergonaut.apps.bots.tests.fakes import fake_registry, say, tool_call
from ergonaut.apps.users.models import ApiKey

SCRIPT = django_ergo.bots.__path__[0] + "/skill_library/ergo-client/scripts/ergonaut_remote.py"

BOT = """
name: kitchen
description: Runs the kitchen
engine: {type: claude}
tools: [tools/pantry.py]
"""

TOOLS = """
from django_ergo.bots import bot_tool

@bot_tool(requires_approval=True)
def order(item: str) -> str:
    return f"ordered {item}"
"""


@pytest.fixture
def ergo(tmp_path, monkeypatch, live_server):
    spec = importlib.util.spec_from_file_location("ergo_client_cli", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "CONFIG", tmp_path / "client.json")
    monkeypatch.delenv("ERGONAUT_URL", raising=False)
    monkeypatch.delenv("ERGONAUT_API_KEY", raising=False)
    user = get_user_model().objects.create_user("cook", "cook@example.com", "pw")
    _, key = ApiKey.issue(user, "test")
    assert module.main(["login", live_server.url, "--name", "test", "--key", key]) == 0
    return module


@pytest.fixture
def kitchen(tmp_path):
    folder = tmp_path / "kitchen"
    (folder / "tools").mkdir(parents=True)
    (folder / "agents.md").write_text("You run the kitchen.")
    (folder / "tools" / "pantry.py").write_text(textwrap.dedent(TOOLS))
    (folder / "bot.yaml").write_text(textwrap.dedent(BOT))

    def install(*responses):
        registry, client = fake_registry(folder, *responses)
        webhooks.set_registry(registry)
        return client

    yield install
    webhooks.set_registry(load_registry)


@pytest.mark.django_db(transaction=True)
def test_ergo_client_runs_a_thread_end_to_end(ergo, kitchen, capsys):
    kitchen(
        say("Hello from the kitchen."),
        tool_call("order", {"item": "eggs"}),
        say("Ordered the eggs.", status="Eggs ordered"),
    )
    assert ergo.main(["whoami"]) == 0
    assert "cook@example.com" in capsys.readouterr().out
    assert ergo.main(["bots"]) == 0
    assert "kitchen: Runs the kitchen" in capsys.readouterr().out

    assert ergo.main(["new", "kitchen", "Say hello", "--title", "Hello", "--wait", "--timeout", "20"]) == 0
    captured = capsys.readouterr()
    assert "Hello from the kitchen." in captured.out
    thread = captured.err.split()[1]

    assert ergo.main(["send", thread[:8], "Order eggs", "--wait", "--timeout", "20"]) == 0
    out = capsys.readouterr().out
    assert "Waiting for approval" in out and "order" in out

    call_id = next(
        line.removeprefix("Approval call ID: ") for line in out.splitlines() if line.startswith("Approval call ID: ")
    )
    batch = next(
        line.removeprefix("Approval batch: ") for line in out.splitlines() if line.startswith("Approval batch: ")
    )
    assert (
        ergo.main(
            ["approve", thread, "--shown-call-id", call_id, "--shown-approvals", batch, "--wait", "--timeout", "20"]
        )
        == 0
    )
    assert "Ordered the eggs." in capsys.readouterr().out

    assert ergo.main(["show", thread]) == 0
    out = capsys.readouterr().out
    assert "→ order(" in out and "Ordered the eggs." in out

    # A message link from the web app shows the messages around its line, marking the linked one.
    url = ergo.load_config()["servers"]["test"]["url"]
    assert ergo.main(["show", f"{url}/s/{thread}#m-0"]) == 0
    out = capsys.readouterr().out
    assert "[0] user:  <- linked message" in out and "Say hello" in out

    assert ergo.main(["threads", "--bot", "kitchen", "--json"]) == 0
    assert thread in capsys.readouterr().out
    assert ergo.main(["close", thread]) == 0


@pytest.mark.django_db(transaction=True)
def test_remote_approval_carries_reviewed_batch_to_tool(ergo, kitchen, capsys, tmp_path):
    from django_ergo.conversation.models import StructuredCall

    (tmp_path / "kitchen" / "tools" / "pantry.py").write_text("""
import json
from django_ergo.bots import bot_tool
@bot_tool(requires_approval=True, takes_context=True)
def order(ctx, item: str) -> str:
    return json.dumps(ctx.approval_decision)
""")
    fake = kitchen(
        say("Hello from the kitchen."),
        tool_call("order", {"item": "eggs"}),
        say("Ordered the eggs.", status="Eggs ordered"),
    )
    assert ergo.main(["new", "kitchen", "Hello", "--title", "Hello", "--wait", "--timeout", "20"]) == 0
    thread = capsys.readouterr().err.split()[1]
    assert ergo.main(["send", thread, "Order eggs", "--wait", "--timeout", "20"]) == 0
    capsys.readouterr()
    assert ergo.main(["show", thread]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert any(line.startswith("Approval call ID: ") for line in lines), "\n".join(lines)
    call_id = next(line.removeprefix("Approval call ID: ") for line in lines if line.startswith("Approval call ID: "))
    batch = next(line.removeprefix("Approval batch: ") for line in lines if line.startswith("Approval batch: "))
    changed = json.loads(batch)
    changed[0]["input"]["item"] = "milk"
    assert ergo.main(["approve", thread, "--shown-call-id", call_id, "--shown-approvals", json.dumps(changed)]) == 1
    assert ergo.main(["approve", thread]) == 1
    assert StructuredCall.objects.get(pk=call_id).status == "awaiting_approval"
    from pathlib import Path
    from types import SimpleNamespace

    spec = importlib.util.spec_from_file_location("remote_tools", Path(SCRIPT).parents[1] / "tools.py")
    tools = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tools)
    url, key = ergo.server("")
    ctx = SimpleNamespace(secret=lambda name: {"ERGONAUT_URL": url, "ERGONAUT_API_KEY": key}[name])
    assert "Approved" in tools.ergo_client_approve(ctx, thread, call_id, batch)
    call = StructuredCall.objects.get(pk=call_id)
    [decision] = call.metadata["authenticated_approval_decisions"]
    [result] = fake.calls[-1]["messages"][-1]["content"]
    assert json.loads(result["content"]) == {
        "id": decision["id"],
        "actor_id": decision["actor_id"],
        "call_id": call_id,
        "tool_use_id": json.loads(batch)[0]["id"],
    }


@pytest.mark.django_db(transaction=True)
def test_ergo_client_reports_a_bad_key(ergo, capsys):
    assert ergo.main(["login", ergo.load_config()["servers"]["test"]["url"], "--key", "ergo_wrong"]) == 1
    assert "HTTP 401" in capsys.readouterr().err


def test_ergonaut_remote_runs_the_client(tmp_path, monkeypatch, capsys):
    from ergonaut.cli import main

    monkeypatch.setenv("ERGONAUT_REMOTE_CONFIG", str(tmp_path / "remote.json"))
    assert main(["remote", "servers"]) == 0
    with pytest.raises(SystemExit):
        main(["remote", "-h"])
    assert "ergonaut-remote" in capsys.readouterr().out
