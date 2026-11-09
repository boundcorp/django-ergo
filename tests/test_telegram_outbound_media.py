"""Sending persistent session files to Telegram."""

import json

import pytest
from django.contrib.auth import get_user_model

from django_ergo.bots.tools import FunctionToolkit
from django_ergo.bots.tools import ToolContext
from django_ergo.conversation.attachments import save_session_file
from django_ergo.conversation.models import ConversationSession
from django_ergo.plugins.telegram import TelegramAPI
from django_ergo.plugins.telegram import TelegramPlugin


class RecordingAPI:
    def __init__(self):
        self.uploads = []

    async def upload(self, method, field, filename, media_type, data, **params):
        self.uploads.append((method, field, filename, media_type, data, params))
        return {"message_id": 42}


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("media_type", "method", "field"),
    [
        ("image/png", "sendPhoto", "photo"),
        ("video/mp4", "sendVideo", "video"),
        ("application/pdf", "sendDocument", "document"),
    ],
)
def test_send_saved_session_file(settings, tmp_path, media_type, method, field):
    settings.MEDIA_ROOT = tmp_path
    user = get_user_model().objects.create_user("lee")
    session = ConversationSession.objects.create(
        user=user,
        bot_name="bot",
        metadata={"telegram": {"chat_id": -123, "message_id": 7}},
    )
    row = save_session_file(session, "asset", b"file bytes", media_type=media_type)
    plugin = TelegramPlugin(None, {})
    plugin.on_load()
    plugin.api = RecordingAPI()
    toolkit = FunctionToolkit(plugin._tools(), ToolContext(session=session, user=user))

    result = toolkit.execute_tool(
        "ergo_telegram_file",
        {
            "attachment_id": str(row.id),
            "caption": "Here",
            "reply": True,
        },
    )

    assert "Sent to Telegram" in result
    assert plugin.api.uploads == [
        (
            method,
            field,
            "asset",
            media_type,
            b"file bytes",
            {
                "chat_id": -123,
                "caption": "Here",
                "reply_parameters": {
                    "message_id": 7,
                    "allow_sending_without_reply": True,
                },
            },
        )
    ]
    row.refresh_from_db()
    with row.file.open("rb") as f:
        assert f.read() == b"file bytes"


@pytest.mark.django_db
def test_send_cannot_access_another_session(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    user = get_user_model().objects.create_user("lee")
    session = ConversationSession.objects.create(
        user=user, bot_name="bot", metadata={"telegram": {"chat_id": 1}}
    )
    other = ConversationSession.objects.create(user=user, bot_name="bot")
    row = save_session_file(other, "secret.txt", b"secret")
    plugin = TelegramPlugin(None, {})
    plugin.on_load()
    plugin.api = RecordingAPI()
    toolkit = FunctionToolkit(plugin._tools(), ToolContext(session=session, user=user))
    with pytest.raises(ValueError, match="No file"):
        toolkit.execute_tool("ergo_telegram_file", {"attachment_id": str(row.id)})
    assert not plugin.api.uploads


def test_multipart_upload_encodes_bytes_and_reply(monkeypatch):
    requests = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def read(self):
            return json.dumps({"ok": True, "result": {"message_id": 9}}).encode()

    def open_request(request, timeout):
        requests.append(request)
        return Response()

    monkeypatch.setattr("urllib.request.urlopen", open_request)
    api = TelegramAPI("fake", "https://example.invalid")
    assert api._upload(
        "sendDocument",
        "document",
        "file.txt",
        "text/plain",
        b"hello",
        {
            "chat_id": 1,
            "reply_parameters": {"message_id": 2},
        },
    ) == {"message_id": 9}
    request = requests[0]
    assert request.full_url.endswith("/sendDocument")
    assert request.get_header("Content-type").startswith(
        "multipart/form-data; boundary="
    )
    assert b'name="document"; filename="file.txt"' in request.data
    assert b"Content-Type: text/plain\r\n\r\nhello\r\n" in request.data
    assert b'{"message_id": 2}' in request.data


@pytest.mark.django_db
def test_send_rejects_oversized_file(settings, tmp_path, monkeypatch):
    settings.MEDIA_ROOT = tmp_path
    monkeypatch.setattr("django_ergo.plugins.telegram.MAX_UPLOAD_BYTES", 3)
    user = get_user_model().objects.create_user("lee")
    session = ConversationSession.objects.create(
        user=user, bot_name="bot", metadata={"telegram": {"chat_id": 1}}
    )
    row = save_session_file(session, "large.txt", b"four")
    plugin = TelegramPlugin(None, {})
    plugin.on_load()
    plugin.api = RecordingAPI()
    toolkit = FunctionToolkit(plugin._tools(), ToolContext(session=session, user=user))
    with pytest.raises(ValueError, match="upload limit"):
        toolkit.execute_tool("ergo_telegram_file", {"attachment_id": str(row.id)})
    assert not plugin.api.uploads


def test_upload_network_error_does_not_expose_token(monkeypatch):
    import urllib.error

    def fail(request, timeout):
        raise urllib.error.URLError(request.full_url)

    monkeypatch.setattr("urllib.request.urlopen", fail)
    api = TelegramAPI("secret-token")
    with pytest.raises(RuntimeError, match="upload failed") as exc:
        api._upload("sendDocument", "document", "file.txt", "text/plain", b"x", {})
    assert "secret-token" not in str(exc.value)
