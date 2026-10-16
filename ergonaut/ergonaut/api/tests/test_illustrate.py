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


def illustration_output():
    return tool_call(
        "submit_output",
        {
            "title": "Dinner decision",
            "summary": "Eggs are available for tacos.",
            "sections": [{"heading": "Decision", "body": "Use four eggs.", "evidence_refs": [1]}],
            "caveats": ["No performance or trace data was supplied."],
        },
    )


@pytest.mark.django_db(transaction=True)
def test_illustration_is_scoped_sanitized_and_historic(client, cook, use_bots, settings, tmp_path):  # noqa: F811
    settings.MEDIA_ROOT = str(tmp_path / "media")
    use_bots(say("Four eggs are available."), suggestion_output(), illustration_output(), illustration_output())
    root = post(client, "/api/bots/kitchen/root").json()
    assert post(client, f"/api/sessions/{root['id']}/messages", {"text": "Should we make tacos?"}).status_code == 200
    suggested = post(client, f"/api/sessions/{root['id']}/illustrations/suggest").json()
    assert suggested["as_of_line"] >= 0
    created = post(
        client,
        f"/api/sessions/{root['id']}/illustrations",
        {"snapshot": suggested["snapshot"], "suggestion_ids": ["timeline"]},
    )
    assert created.status_code == 200, created.content
    first = created.json()["attachment"]
    assert re.fullmatch(r"illustration-\d{8}-\d{6}-dinner-decision-[0-9a-f]{8}\.html", first["filename"])
    content = client.get(f"/api/attachments/{first['id']}/download?inline=true")
    assert content.status_code == 200
    assert b"Use four eggs." in content.content
    assert content["Content-Security-Policy"].startswith("sandbox")
    again = post(
        client,
        f"/api/sessions/{root['id']}/illustrations",
        {"snapshot": suggested["snapshot"], "suggestion_ids": ["timeline"]},
    )
    assert again.status_code == 200
    files = client.get(f"/api/sessions/{root['id']}/attachments").json()
    assert {first["id"], again.json()["attachment"]["id"]} <= {file["id"] for file in files}
    assert first["filename"] != again.json()["attachment"]["filename"]


@pytest.mark.django_db(transaction=True)
def test_custom_and_combined_illustrations_validate_scope_and_permissions(client, cook, use_bots):  # noqa: F811
    use_bots(say("Four eggs are available."), suggestion_output(), illustration_output(), illustration_output())
    root = post(client, "/api/bots/kitchen/root").json()
    post(client, f"/api/sessions/{root['id']}/messages", {"text": "Should we make tacos?"})
    suggested = post(client, f"/api/sessions/{root['id']}/illustrations/suggest").json()
    assert (
        post(
            client,
            f"/api/sessions/{root['id']}/illustrations",
            {"snapshot": suggested["snapshot"], "suggestion_ids": ["status"]},
        ).status_code
        == 400
    )
    created = post(
        client,
        f"/api/sessions/{root['id']}/illustrations",
        {"snapshot": suggested["snapshot"], "custom": "Show the taco choice as a simple map"},
    )
    assert created.status_code == 200, created.content
    combined = post(
        client,
        f"/api/sessions/{root['id']}/illustrations",
        {
            "snapshot": suggested["snapshot"],
            "suggestion_ids": ["timeline"],
            "custom": "Show the taco choice as a simple map",
        },
    )
    assert combined.status_code == 200, combined.content
    assert combined.json()["attachment"]["filename"].endswith(".html")
    assert (
        post(
            client,
            f"/api/sessions/{root['id']}/illustrations",
            {"snapshot": suggested["snapshot"], "custom": "x" * 501},
        ).status_code
        == 400
    )
    other = get_user_model().objects.create_user("other", "o@example.com", "pw")
    client.force_login(other)
    assert post(client, f"/api/sessions/{root['id']}/illustrations/suggest").status_code == 404


def test_static_illustration_escapes_model_text():
    from ergonaut.api.illustrate import IllustrationSection, IllustrationSpec, _render_html

    html = _render_html(
        "status",
        IllustrationSpec(
            title="<script>alert(1)</script>",
            summary="Safe <b>text</b>",
            sections=[IllustrationSection(heading="Facts", body="<img src=x onerror=alert(1)>")],
        ),
        3,
    ).decode()
    assert "<script>" not in html
    assert "&lt;img src=x onerror=alert(1)&gt;" in html


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
