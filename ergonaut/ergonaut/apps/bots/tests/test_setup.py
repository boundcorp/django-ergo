import json

import pytest
from django.contrib.auth import get_user_model
from django_ergo.conversation.models import ConversationSession

from ergonaut.utils.admin import admin_site


def test_ergo_models_are_in_the_admin():
    assert admin_site.is_registered(ConversationSession)


@pytest.mark.django_db
def test_unknown_bot_webhook_is_404(client):
    response = client.post("/hooks/nobody/telegram/update/", json.dumps({}), content_type="application/json")
    assert response.status_code == 404


@pytest.mark.django_db
def test_ergo_tables_exist():
    user = get_user_model().objects.create(username="lee", email="lee@example.com")
    session = ConversationSession.objects.create(user=user, bot_name="kitchen", engine_type="claude")
    assert session.bot_name == "kitchen"
