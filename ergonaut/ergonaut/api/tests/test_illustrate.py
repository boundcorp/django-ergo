# ruff: noqa: F811

import json
import re
from types import SimpleNamespace
from uuid import uuid4

import pytest
from django.contrib.auth import get_user_model

from ergonaut.apps.bots.tests.fakes import say, tool_call
from ergonaut.apps.bots.tests.test_api import bot_folder, cook, post, use_bots  # noqa: F401


def suggestion_output():
    return tool_call(
        "submit_output",
        {
            "suggestions": [
                {
                    "id": "timeline",
                    "title": "Dinner decision timeline",
                    "purpose": "Show the choices in order.",
                    "kind": "timeline",
                    "evidence_refs": [1],
                    "availability": "available",
                    "reason": "The chat contains decisions.",
                }
            ]
        },
    )


def illustration_output(*topics):
    """A small valid spec that serves every topic with two visual blocks and one prose block."""
    topics = topics or ("timeline",)
    blocks = []
    for topic in topics:
        blocks += [
            {
                "type": "timeline",
                "topic": topic,
                "heading": f"{topic} steps",
                "items": [
                    {"title": "Asked about tacos", "state": "done", "evidence_refs": [1]},
                    {"title": "Checked the eggs", "state": "in_progress"},
                ],
            },
            {
                "type": "status_board",
                "topic": topic,
                "heading": f"{topic} status",
                "items": [
                    {"title": "Four eggs on hand", "state": "done", "evidence_refs": [1]},
                    {"title": "Pick a filling", "state": "open"},
                ],
            },
            {"type": "prose", "topic": topic, "heading": "Decision", "body": "Use four eggs.", "evidence_refs": [1]},
        ]
    return tool_call(
        "submit_output",
        {
            "title": "Dinner decision",
            "summary": "Eggs are available for tacos.",
            "takeaways": ["Four eggs are on hand."],
            "blocks": blocks,
            "caveats": ["No performance or trace data was supplied."],
        },
    )


def queue_illustrations(settings, monkeypatch):
    from types import SimpleNamespace

    from ergonaut.apps.bots.tasks import generate_illustration

    settings.CELERY_BROKER_URL = "redis://illustrations"
    settings.CELERY_TASK_ALWAYS_EAGER = False
    monkeypatch.setattr(generate_illustration, "delay", lambda *_args: SimpleNamespace(id="illustration-task"))


def create_illustration(client, session_id, payload):
    from ergonaut.api.illustrate import run_illustration_job
    from ergonaut.apps.bots.models import IllustrationJob

    response = post(client, f"/api/sessions/{session_id}/illustrations", payload)
    assert response.status_code == 202, response.content
    job = IllustrationJob.objects.get(id=response.json()["id"])
    run_illustration_job(str(job.id), str(job.run_token))
    return client.get(f"/api/sessions/{session_id}/illustrations/{job.id}")


@pytest.mark.parametrize("bot_name", ["personal", "kitchen"])
def test_suggestions_use_session_bound_ancillary_engine(monkeypatch, bot_name):
    from ergonaut.api import illustrate

    session = SimpleNamespace(id=uuid4(), bot_name=bot_name)
    user = object()
    engine = object()
    calls = []
    messages = [{"line": 1, "role": "user", "text": "Private discussion"}]
    monkeypatch.setattr(illustrate, "_brief", lambda _: (messages, 1))

    def make_ancillary_engine(source, *, purpose):
        calls.append((source, purpose))
        return engine

    monkeypatch.setattr(
        illustrate,
        "get_bot",
        lambda name, owner: SimpleNamespace(
            make_ancillary_engine=make_ancillary_engine,
            private_pool=object() if name == "personal" else None,
        ),
    )

    async def run(spec, prompt, **kwargs):
        assert spec.kind == "illustrate_suggest"
        assert kwargs["engine"] is engine
        assert kwargs["user"] is user
        assert "Private discussion" in prompt
        return SimpleNamespace(parsed=illustrate.SuggestionList(suggestions=illustrate._fallback_suggestions([1])))

    monkeypatch.setattr(illustrate, "run_structured_call", run)
    suggestions, _, _ = illustrate._suggest(session, user, "")
    assert suggestions
    assert calls == [(session, "illustrate_suggest")]


@pytest.mark.parametrize("error", ["cap", "provider"])
def test_personal_suggestion_failure_does_not_fall_back(monkeypatch, error):
    from ninja.errors import HttpError

    from ergonaut.api import illustrate

    session = SimpleNamespace(id=uuid4(), bot_name="personal")
    engine = object()
    calls = []
    monkeypatch.setattr(illustrate, "_brief", lambda _: ([{"line": 1, "role": "user", "text": "secret"}], 1))
    monkeypatch.setattr(
        illustrate,
        "get_bot",
        lambda *_: SimpleNamespace(make_ancillary_engine=lambda *args, **kwargs: engine, private_pool=object()),
    )

    async def run(spec, prompt, **kwargs):
        calls.append(kwargs["engine"])
        return SimpleNamespace(parsed=None, call=SimpleNamespace(error=error))

    monkeypatch.setattr(illustrate, "run_structured_call", run)
    with pytest.raises(HttpError) as exc:
        illustrate._suggest(session, object(), "")
    assert exc.value.status_code == 503
    assert calls == [engine]


def test_suggestion_policy_refusal_does_not_send_or_retry(monkeypatch):
    from django_ergo.conversation.inference_policy import PolicyError

    from ergonaut.api import illustrate

    session = SimpleNamespace(id=uuid4(), bot_name="kitchen")
    monkeypatch.setattr(illustrate, "_brief", lambda _: ([{"line": 1, "role": "user", "text": "secret"}], 1))

    def refuse(*args, **kwargs):
        raise PolicyError("private route refused")

    monkeypatch.setattr(illustrate, "get_bot", lambda *_: SimpleNamespace(make_ancillary_engine=refuse))

    async def unexpected_send(*args, **kwargs):
        pytest.fail("policy refusal must prevent the structured call")

    monkeypatch.setattr(illustrate, "run_structured_call", unexpected_send)
    with pytest.raises(PolicyError):
        illustrate._suggest(session, object(), "")


@pytest.mark.django_db(transaction=True)
def test_illustration_is_scoped_sanitized_and_historic(client, cook, use_bots, settings, tmp_path, monkeypatch):
    settings.MEDIA_ROOT = str(tmp_path / "media")
    fake = use_bots(say("Four eggs are available."), suggestion_output(), illustration_output())
    root = post(client, "/api/bots/kitchen/root").json()
    assert post(client, f"/api/sessions/{root['id']}/messages", {"text": "Should we make tacos?"}).status_code == 200
    queue_illustrations(settings, monkeypatch)
    suggested = post(client, f"/api/sessions/{root['id']}/illustrations/suggest").json()
    assert suggested["as_of_line"] >= 0
    completed = create_illustration(
        client, root["id"], {"snapshot": suggested["snapshot"], "suggestion_ids": ["timeline"]}
    )
    assert completed.status_code == 200, completed.content
    first = completed.json()["attachment"]
    assert re.fullmatch(r"illustration-\d{8}-\d{6}-dinner-decision-[0-9a-f]{8}\.html", first["filename"])
    content = client.get(f"/api/attachments/{first['id']}/download?inline=true")
    assert content.status_code == 200
    assert b"Use four eggs." in content.content
    assert b'class="timeline"' in content.content
    # The page designer is given the suggestion the user ticked, not just its id.
    from ergonaut.apps.bots.models import IllustrationJob

    job = IllustrationJob.objects.get(id=completed.json()["id"])
    assert job.topics[0]["title"] == "Dinner decision timeline"
    assert job.topics[0]["purpose"] == "Show the choices in order."
    generate_call = fake.calls[-1]
    assert generate_call["max_tokens"] >= 8_000
    assert "Show the choices in order." in json.dumps(generate_call["messages"])
    assert content["Content-Security-Policy"].startswith("sandbox")
    again = post(
        client,
        f"/api/sessions/{root['id']}/illustrations",
        {"snapshot": suggested["snapshot"], "suggestion_ids": ["timeline"]},
    )
    assert again.status_code == 202
    assert again.json()["status"] == "succeeded"
    assert again.json()["attachment"]["id"] == first["id"]
    files = client.get(f"/api/sessions/{root['id']}/attachments").json()
    assert [file["id"] for file in files].count(first["id"]) == 1


@pytest.mark.django_db(transaction=True)
def test_custom_and_combined_illustrations_validate_scope_and_permissions(
    client, cook, use_bots, settings, monkeypatch
):
    use_bots(
        say("Four eggs are available."),
        suggestion_output(),
        illustration_output("custom"),
        illustration_output("timeline", "custom"),
    )
    root = post(client, "/api/bots/kitchen/root").json()
    post(client, f"/api/sessions/{root['id']}/messages", {"text": "Should we make tacos?"})
    queue_illustrations(settings, monkeypatch)
    suggested = post(client, f"/api/sessions/{root['id']}/illustrations/suggest").json()
    invalid_selection = post(
        client,
        f"/api/sessions/{root['id']}/illustrations",
        {"snapshot": suggested["snapshot"], "suggestion_ids": ["timeline"] * 4},
    )
    assert invalid_selection.status_code == 400
    assert invalid_selection.json()["detail"] == "Choose up to three current illustration suggestions"
    created = create_illustration(
        client, root["id"], {"snapshot": suggested["snapshot"], "custom": "Show the taco choice as a simple map"}
    )
    assert created.status_code == 200, created.content
    combined = create_illustration(
        client,
        root["id"],
        {
            "snapshot": suggested["snapshot"],
            "suggestion_ids": ["timeline"],
            "custom": "Show the taco choice as a simple map",
        },
    )
    assert combined.status_code == 200, combined.content
    assert combined.json()["attachment"]["filename"].endswith(".html")
    custom_too_long = post(
        client,
        f"/api/sessions/{root['id']}/illustrations",
        {"snapshot": suggested["snapshot"], "custom": "x" * 501},
    )
    assert custom_too_long.status_code == 400
    assert custom_too_long.json()["detail"] == "Custom illustration request is limited to 500 characters"
    other = get_user_model().objects.create_user("other", "o@example.com", "pw")
    client.force_login(other)
    assert post(client, f"/api/sessions/{root['id']}/illustrations/suggest").status_code == 404


@pytest.mark.django_db(transaction=True)
def test_illustration_job_retries_times_out_and_cancels(client, cook, use_bots, settings, monkeypatch):
    from ergonaut.api.illustrate import mark_illustration_retry, run_illustration_job
    from ergonaut.apps.bots.models import IllustrationJob

    use_bots(say("Four eggs are available."), suggestion_output(), illustration_output())
    root = post(client, "/api/bots/kitchen/root").json()
    post(client, f"/api/sessions/{root['id']}/messages", {"text": "Should we make tacos?"})
    queue_illustrations(settings, monkeypatch)
    suggested = post(client, f"/api/sessions/{root['id']}/illustrations/suggest").json()
    issued = post(
        client,
        f"/api/sessions/{root['id']}/illustrations",
        {"snapshot": suggested["snapshot"], "suggestion_ids": ["timeline"]},
    )
    assert issued.status_code == 202
    job = IllustrationJob.objects.get(id=issued.json()["id"])
    token = str(job.run_token)
    other = get_user_model().objects.create_user("other-job", "other-job@example.com", "pw")
    client.force_login(other)
    assert client.get(f"/api/sessions/{root['id']}/illustrations/{job.id}").status_code == 404
    client.force_login(cook)
    assert mark_illustration_retry(str(job.id), token, "SoftTimeLimitExceeded", final=False)
    assert client.get(f"/api/sessions/{root['id']}/illustrations/{job.id}").json()["status"] == "retrying"
    assert not mark_illustration_retry(str(job.id), token, "SoftTimeLimitExceeded", final=True)
    timed_out = client.get(f"/api/sessions/{root['id']}/illustrations/{job.id}").json()
    assert timed_out["status"] == "failed"
    assert timed_out["error"] == "Illustration generation timed out."

    retried = post(
        client,
        f"/api/sessions/{root['id']}/illustrations",
        {"snapshot": suggested["snapshot"], "suggestion_ids": ["timeline"]},
    )
    assert retried.status_code == 202
    job.refresh_from_db()
    assert str(job.run_token) != token
    cancelled = client.delete(f"/api/sessions/{root['id']}/illustrations/{job.id}")
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"
    run_illustration_job(str(job.id), str(job.run_token))
    assert not IllustrationJob.objects.get(id=job.id).attachment_id


def test_snapshot_keeps_the_selected_messages():
    from ergonaut.api.illustrate import Suggestion, _load_snapshot, _snapshot

    session = SimpleNamespace(id=uuid4())
    messages = [{"line": 4, "role": "user", "text": "Use the first decision."}]
    token = _snapshot(
        session,
        messages,
        4,
        [
            Suggestion(
                id="timeline",
                title="Timeline",
                purpose="Show the decision.",
                kind="timeline",
                reason="The chat contains a decision.",
            )
        ],
    )
    snapshot = _load_snapshot(token, session)
    assert snapshot["messages"] == messages
    assert snapshot["digest"]
