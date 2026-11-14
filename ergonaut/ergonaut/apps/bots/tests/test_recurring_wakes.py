import json
import uuid
from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.core.serializers.json import DjangoJSONEncoder
from django.utils import timezone
from django_ergo.bots import recurring_wakes, webhooks
from django_ergo.conversation.models import ConversationSession

from ergonaut.api import stream
from ergonaut.apps.bots.loading import load_registry
from ergonaut.apps.bots.tests.fakes import fake_registry


@pytest.fixture
def owner(client):
    user = get_user_model().objects.create_user("cook")
    client.force_login(user)
    return user


@pytest.fixture
def install_bot(tmp_path):
    def install(*, allowed_users=None):
        folder = tmp_path / "kitchen"
        folder.mkdir(exist_ok=True)
        (folder / "agents.md").write_text("You run the kitchen.")
        config = "name: kitchen\nengine: {type: claude}\norchestration: true\n"
        if allowed_users is not None:
            config += f"permissions: {{users: [{', '.join(allowed_users)}]}}\n"
        (folder / "bot.yaml").write_text(config)
        registry, _ = fake_registry(folder)
        webhooks.set_registry(registry)

    yield install
    webhooks.set_registry(load_registry)


@pytest.fixture
def thread(owner, install_bot):
    install_bot()
    root = ConversationSession.objects.create(user=owner, bot_name="kitchen", metadata={"bot_role": "main"})
    return ConversationSession.objects.create(
        user=owner, bot_name="kitchen", parent=root, metadata={"bot_role": "thread"}
    )


def schedule_for(thread):
    return recurring_wakes.create(
        thread,
        "Check the oven",
        interval_minutes=5,
        expires_at=timezone.now() + timedelta(hours=2),
        max_occurrences=12,
    )


def control(client, thread, schedule_id, action):
    return client.post(
        f"/api/sessions/{thread.id}/recurring-wakes/{schedule_id}",
        json.dumps({"action": action}),
        content_type="application/json",
    )


@pytest.mark.django_db
def test_owner_can_list_pause_resume_and_cancel(client, thread):
    schedule = schedule_for(thread)
    url = f"/api/sessions/{thread.id}/recurring-wakes"
    listed = client.get(url)
    assert listed.status_code == 200
    assert listed.json() == json.loads(json.dumps([schedule], cls=DjangoJSONEncoder))
    assert client.get(f"/api/sessions/{thread.id}").json()["recurring_wakes"] == listed.json()
    assert listed.json()[0]["periodic_checkins"] is False
    assert listed.json()[0]["interval_seconds"] == 300
    assert listed.json()[0]["max_occurrences"] == 12

    for action, status in (("pause", "paused"), ("resume", "active"), ("cancel", "cancelled")):
        response = control(client, thread, schedule["id"], action)
        assert response.status_code == 200
        assert response.json()["id"] == schedule["id"]
        assert response.json()["status"] == status
        assert client.get(url).json()[0]["status"] == status
    response = control(client, thread, schedule["id"], "resume")
    assert response.status_code == 409
    assert response.json()["detail"]


@pytest.mark.django_db
def test_recurring_controls_require_login_owner_and_bot_access(client, thread, install_bot):
    schedule = schedule_for(thread)
    url = f"/api/sessions/{thread.id}/recurring-wakes"
    client.logout()
    assert client.get(url).status_code == 401
    assert control(client, thread, schedule["id"], "cancel").status_code == 401

    other = get_user_model().objects.create_user("other")
    client.force_login(other)
    assert client.get(url).status_code == 404
    for action in ("pause", "resume", "cancel"):
        assert control(client, thread, schedule["id"], action).status_code == 404

    client.force_login(thread.user)
    install_bot(allowed_users=["other"])
    assert client.get(url).status_code == 404
    assert control(client, thread, schedule["id"], "cancel").status_code == 404
    assert recurring_wakes.list_schedules(thread)[0]["status"] == "active"


@pytest.mark.django_db
def test_control_rejects_schedule_from_another_thread_and_invalid_actions(client, thread):
    schedule = schedule_for(thread)
    another = ConversationSession.objects.create(
        user=thread.user, bot_name="kitchen", parent=thread.parent, metadata={"bot_role": "thread"}
    )
    assert control(client, another, schedule["id"], "cancel").status_code == 404
    assert control(client, thread, str(uuid.uuid4()), "cancel").status_code == 404
    assert control(client, thread, "not-a-uuid", "cancel").status_code == 404
    assert control(client, thread, schedule["id"], "delete").status_code == 422
    assert recurring_wakes.list_schedules(thread)[0]["status"] == "active"


@pytest.mark.django_db
def test_stream_sends_recurring_changes_without_messages(client, thread):
    schedule = schedule_for(thread)
    seen = {}
    first = stream._snapshot(thread.id, -1, seen)[-1]
    first_json = json.loads(stream._event(first).partition("data: ")[2])
    assert first_json["recurring_wakes"] == client.get(f"/api/sessions/{thread.id}/recurring-wakes").json()
    assert "recurring_wakes" not in stream._snapshot(thread.id, -1, seen)[-1]

    assert control(client, thread, schedule["id"], "pause").status_code == 200
    update = stream._snapshot(thread.id, -1, seen)
    assert update[0] == []
    assert update[-1]["recurring_wakes"][0]["status"] == "paused"
    assert "recurring_wakes" not in stream._snapshot(thread.id, -1, seen)[-1]
