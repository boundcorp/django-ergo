"""Privacy, ownership and terminal-race invariants for voice lifecycle evidence."""

# ruff: noqa: F811 -- shared pytest fixture imports.

import json
from datetime import timedelta
from unittest.mock import patch

import pytest

from ergonaut.api import realtime_voice
from ergonaut.api.tests.test_realtime_voice import create, post, voice_setup  # noqa: F401
from ergonaut.apps.bots.models import RealtimeVoiceSession
from ergonaut.realtime_voice_diagnostics import record_lifecycle

pytestmark = pytest.mark.django_db


def test_old_claim_cannot_append_and_teardown_does_not_replace_first_provider_close(voice_setup):
    _, client, chat = voice_setup
    voice, url = create(client, chat)
    vid = voice["id"]
    assert realtime_voice.claim_bridge(vid, "current")
    assert record_lifecycle(vid, "stale", "provider_closed", reason="expired") is None
    first = record_lifecycle(vid, "current", "provider_closed", reason="expired")
    post(client, url + "/end", {})
    replay = record_lifecycle(vid, "current", "provider_closed", reason="close_requested", teardown=True)
    assert replay.pk == first.pk
    assert json.loads(replay.text)["reason"] == "expired"
    assert json.loads(replay.text)["teardown"] is False
    record_lifecycle(vid, "current", "provider_close_sent", teardown=True)
    session = RealtimeVoiceSession.objects.get(pk=vid)
    assert session.status == "ended"
    assert {json.loads(e.text)["boundary"] for e in session.events.filter(kind="lifecycle")} == {
        "provider_closed",
        "provider_close_sent",
        "session_ended",
    }
    assert not chat.messages.exists()


def test_unrecognized_provider_payload_is_never_retained(voice_setup):
    _, client, chat = voice_setup
    voice, _ = create(client, chat)
    assert realtime_voice.claim_bridge(voice["id"], "claim")
    event = record_lifecycle(
        voice["id"],
        "claim",
        "provider_closed",
        reason="private transcript or credential",
        error_type="private exception message",
        code=999999,
    )
    data = json.loads(event.text)
    assert data["reason"] == "unknown"
    assert data["exception_class"] == "other"
    assert "wire_close_code" not in data
    assert "private" not in event.text


def test_owner_disconnect_report_survives_end_race_but_never_reopens_voice(voice_setup, django_user_model):
    _, client, chat = voice_setup
    voice, url = create(client, chat)
    assert realtime_voice.claim_bridge(voice["id"], "claim")
    post(client, url + "/end", {})
    payload = {"event_id": "late-close", "boundary": "browser_disconnected", "reason": 1, "visibility": "hidden"}
    assert post(client, url + "/client-lifecycle", payload).json() == {"recorded": True}
    assert post(client, url + "/client-lifecycle", payload).json() == {"recorded": True}
    session = RealtimeVoiceSession.objects.get(pk=voice["id"])
    events = session.events.filter(event_id="lifecycle:browser:late-close")
    assert events.count() == 1
    data = json.loads(events.get().text)
    assert data["reason"] == "CLIENT_INITIATED"  # SDK enum, NOT a claim of a user click.
    assert data["visibility"] == "hidden"
    assert data["source"] == "browser"
    assert session.status == "ended"
    assert not realtime_voice.renew_bridge_lease(voice["id"], "claim")
    with patch(
        "ergonaut.realtime_voice_diagnostics.timezone.now", return_value=session.ended_at + timedelta(seconds=121)
    ):
        assert post(client, url + "/client-lifecycle", {**payload, "event_id": "too-late"}).json() == {
            "recorded": False
        }
    other = django_user_model.objects.create_user("other-lifecycle", password="pw")
    client.force_login(other)
    assert post(client, url + "/client-lifecycle", payload).status_code == 404


def test_browser_observation_limit_reserves_one_terminal_report(voice_setup):
    _, client, chat = voice_setup
    voice, url = create(client, chat)
    for index in range(40):
        response = post(
            client,
            url + "/client-lifecycle",
            {
                "event_id": f"visibility-{index}",
                "boundary": "browser_visibility",
                "visibility": "hidden",
            },
        )
        assert response.json() == {"recorded": index < 39}
    assert post(
        client,
        url + "/client-lifecycle",
        {
            "event_id": "terminal",
            "boundary": "browser_disconnected",
            "reason": 14,
            "visibility": "hidden",
        },
    ).json() == {"recorded": True}
    assert (
        post(
            client,
            url + "/client-lifecycle",
            {
                "event_id": "forged",
                "boundary": "session_ended",
                "visibility": "visible",
            },
        ).status_code
        == 400
    )
    assert RealtimeVoiceSession.objects.get(pk=voice["id"]).status == "active"


def test_diagnostic_storage_failure_does_not_block_explicit_end(voice_setup):
    _, client, chat = voice_setup
    voice, url = create(client, chat)
    with patch("ergonaut.realtime_voice_diagnostics._record_lifecycle", side_effect=RuntimeError("private")):
        assert post(client, url + "/end", {}).json()["status"] == "ended"
