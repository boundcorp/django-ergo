import pytest
from django.contrib.auth import get_user_model
from django_ergo.bots import webhooks
from django_ergo.conversation.models import ConversationSession

from ergonaut.apps.bots.loading import load_registry
from ergonaut.apps.bots.tests.fakes import fake_registry


@pytest.fixture
def install_devbox(tmp_path):
    def install(*, orchestration=True, allowed_users=None, name="devbox"):
        folder = tmp_path / name
        folder.mkdir(exist_ok=True)
        (folder / "agents.md").write_text("You help debug chats.")
        config = f"name: {name}\nengine: {{type: claude}}\norchestration: {str(orchestration).lower()}\n"
        if allowed_users is not None:
            config += f"permissions: {{users: [{', '.join(allowed_users)}]}}\n"
        (folder / "bot.yaml").write_text(config)
        registry, _ = fake_registry(folder)
        webhooks.set_registry(registry)

    yield install
    webhooks.set_registry(load_registry)


@pytest.mark.django_db
def test_shortcut_returns_private_link_without_side_effects(client, settings, install_devbox):
    user = get_user_model().objects.create_user("cook")
    source = ConversationSession.objects.create(user=user, bot_name="kitchen", engine_type="claude")
    install_devbox()
    settings.ERGONAUT_DEBUG_USERS = {"cook"}
    client.force_login(user)

    assert client.get(f"/api/sessions/{source.id}").json()["debug_shortcut"] is True
    before_sessions = ConversationSession.objects.count()
    before_messages = source.messages.count()
    response = client.get(f"/api/sessions/{source.id}/debug-shortcut")
    assert response.status_code == 200
    assert response.json() == {"url": f"/s/{source.id}"}
    assert ConversationSession.objects.count() == before_sessions
    assert source.messages.count() == before_messages


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("allowlist", "orchestration", "allowed_users", "name"),
    [
        (set(), True, None, "devbox"),
        ({"someone-else"}, True, None, "devbox"),
        ({"cook"}, False, None, "devbox"),
        ({"cook"}, True, ["someone-else"], "devbox"),
        ({"cook"}, True, None, "not-devbox"),
    ],
)
def test_shortcut_denied_when_not_available(
    client, settings, install_devbox, allowlist, orchestration, allowed_users, name
):
    user = get_user_model().objects.create_user("cook")
    source = ConversationSession.objects.create(user=user, bot_name="kitchen", engine_type="claude")
    install_devbox(orchestration=orchestration, allowed_users=allowed_users, name=name)
    settings.ERGONAUT_DEBUG_USERS = allowlist
    client.force_login(user)

    assert client.get(f"/api/sessions/{source.id}").json()["debug_shortcut"] is False
    assert client.get(f"/api/sessions/{source.id}/debug-shortcut").status_code == 404


@pytest.mark.django_db
def test_shortcut_requires_source_owner_even_for_admin(client, settings, install_devbox):
    user = get_user_model().objects.create_user("cook")
    admin = get_user_model().objects.create_superuser("admin", "admin@example.com", "pw")
    source = ConversationSession.objects.create(user=user, bot_name="kitchen", engine_type="claude")
    admin_source = ConversationSession.objects.create(user=admin, bot_name="kitchen", engine_type="claude")
    install_devbox()
    settings.ERGONAUT_DEBUG_USERS = {"cook", "admin"}
    url = f"/api/sessions/{source.id}/debug-shortcut"

    assert client.get(url).status_code == 401
    client.force_login(admin)
    assert client.get(f"/api/sessions/{source.id}").json()["debug_shortcut"] is False
    assert client.get(url).status_code == 404
    assert client.get(f"/api/sessions/{admin_source.id}").json()["debug_shortcut"] is True
    settings.ERGONAUT_DEBUG_USERS = {"cook"}
    assert client.get(f"/api/sessions/{admin_source.id}").json()["debug_shortcut"] is False
    assert client.get(f"/api/sessions/{admin_source.id}/debug-shortcut").status_code == 404
    client.force_login(user)
    assert client.get(f"/api/sessions/{admin_source.id}").status_code == 404
    assert client.get(f"/api/sessions/{admin_source.id}/debug-shortcut").status_code == 404
    assert client.get("/api/sessions/not-a-uuid/debug-shortcut").status_code == 404
