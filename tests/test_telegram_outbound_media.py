"""Sending persistent session files to Telegram."""

import json

import pytest
from django.contrib.auth import get_user_model

from django_ergo.bots.orchestrator import orchestrator_toolkit
from django_ergo.bots.tools import FunctionToolkit
from django_ergo.bots.tools import ToolContext
from django_ergo.conversation.attachments import save_session_file
from django_ergo.conversation.models import ConversationAttachment
from django_ergo.conversation.models import ConversationSession
from django_ergo.conversation.models import ThreadMessage
from django_ergo.plugins.telegram import TelegramAPI
from django_ergo.plugins.telegram import TelegramPlugin
from tests.test_bots import _family
from tests.test_conversation_structured import claude_engine


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


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("filename", "media_type", "method", "field"),
    [
        ("mockup.png", "image/png", "sendPhoto", "photo"),
        ("clip.mp4", "video/mp4", "sendVideo", "video"),
        ("report.pdf", "application/pdf", "sendDocument", "document"),
    ],
)
def test_send_file_shared_by_another_bot(  # noqa: PLR0913
    settings, tmp_path, filename, media_type, method, field
):
    settings.MEDIA_ROOT = tmp_path / "media"
    user = get_user_model().objects.create_user("shared")
    _, destination_bot, source_bot = _family(
        tmp_path,
        claude_engine(),
        "name: boundcorp\nplugins: [{name: attachments}]\n",
        "name: design\n",
    )
    source = ConversationSession.objects.create(user=user, bot_name=source_bot.name)
    destination = ConversationSession.objects.create(
        user=user,
        bot_name=destination_bot.name,
        metadata={"telegram": {"chat_id": -987, "message_id": 21}},
    )
    content = b"\x00shared file\xff\n"
    row = save_session_file(source, filename, content, media_type=media_type)
    original_path = row.file.name
    sender = orchestrator_toolkit(
        ToolContext(bot=source_bot, session=source, user=user)
    )
    sent = json.loads(
        sender.execute_tool(
            "ergo_thread_send",
            {
                "bot": destination_bot.name,
                "thread": str(destination.id),
                "message": "Please send this report",
                "attachments": [str(row.id)],
            },
        )
    )
    message = ThreadMessage.objects.get(id=sent["message_id"])
    assert message.recipient_session_id == destination.id
    reference = message.metadata["attachments"][0]
    assert reference["id"] == str(row.id)
    assert reference["session"] == str(source.id)

    plugin = TelegramPlugin(destination_bot, {})
    plugin.on_load()
    plugin.api = RecordingAPI()
    toolkit = FunctionToolkit(
        plugin._tools(),
        ToolContext(bot=destination_bot, session=destination, user=user),
    )
    result = toolkit.execute_tool(
        "ergo_telegram_file",
        {
            "attachment_id": reference["id"],
            "session_id": reference["session"],
            "caption": "Report",
            "reply": True,
        },
    )
    assert "Sent to Telegram" in result
    assert plugin.api.uploads == [
        (
            method,
            field,
            filename,
            media_type,
            content,
            {
                "chat_id": -987,
                "caption": "Report",
                "reply_parameters": {
                    "message_id": 21,
                    "allow_sending_without_reply": True,
                },
            },
        )
    ]
    message.refresh_from_db()
    assert message.metadata["attachments"][0] == reference
    row.refresh_from_db()
    assert row.session_id == source.id
    assert row.file.name == original_path
    with row.file.open("rb") as file:
        assert file.read() == content
    assert not ConversationAttachment.objects.filter(session=destination).exists()

    # A bare ID never grants implicit access to another session, even after sharing.
    with pytest.raises(ValueError, match="No file"):
        toolkit.execute_tool("ergo_telegram_file", {"attachment_id": str(row.id)})

    unshared = save_session_file(source, "unshared.txt", b"not shared")
    with pytest.raises(ValueError, match="No file"):
        toolkit.execute_tool("ergo_telegram_file", {"attachment_id": str(unshared.id)})

    other_user = get_user_model().objects.create_user("private")
    private_session = ConversationSession.objects.create(
        user=other_user, bot_name=source_bot.name
    )
    private = save_session_file(private_session, "private.txt", b"private")
    with pytest.raises(ValueError, match="No session"):
        toolkit.execute_tool(
            "ergo_telegram_file",
            {"attachment_id": str(private.id), "session_id": str(private_session.id)},
        )
    with pytest.raises(ValueError, match="No file"):
        toolkit.execute_tool(
            "ergo_telegram_file",
            {"attachment_id": str(private.id), "session_id": str(source.id)},
        )
    destination_bot.plugin("attachments").other_sessions = False
    with pytest.raises(ValueError, match="only read files in its own session"):
        toolkit.execute_tool(
            "ergo_telegram_file",
            {"attachment_id": reference["id"], "session_id": reference["session"]},
        )
    destination_bot.plugins.remove(destination_bot.plugin("attachments"))
    with pytest.raises(ValueError, match="requires the attachments plugin"):
        toolkit.execute_tool(
            "ergo_telegram_file",
            {"attachment_id": reference["id"], "session_id": reference["session"]},
        )
    assert len(plugin.api.uploads) == 1


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


class DeletingAPI(RecordingAPI):
    def __init__(self):
        super().__init__()
        self.calls = []
        self.next_id = 100

    async def call(self, method, **params):
        self.calls.append((method, params))
        if method == "sendMessage":
            self.next_id += 1
            return {"message_id": self.next_id}
        return True

    async def upload(self, method, field, filename, media_type, data, **params):
        await super().upload(method, field, filename, media_type, data, **params)
        self.next_id += 1
        return {"message_id": self.next_id}


def _delete_setup(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    user = get_user_model().objects.create_user("lee")
    session = ConversationSession.objects.create(
        user=user,
        bot_name="bot",
        metadata={"telegram": {"chat_id": -123, "message_id": 7, "title": "Group"}},
    )
    plugin = TelegramPlugin(None, {})
    plugin.on_load()
    plugin.api = DeletingAPI()
    toolkit = FunctionToolkit(plugin._tools(), ToolContext(session=session, user=user))
    return session, plugin, toolkit


@pytest.mark.django_db
def test_delete_latest_sent_message(settings, tmp_path):
    session, plugin, toolkit = _delete_setup(settings, tmp_path)
    row = save_session_file(session, "clip.mp4", b"v", media_type="video/mp4")

    assert "message 101" in toolkit.execute_tool(
        "ergo_telegram_message", {"text": "hi"}
    )
    assert "message 102" in toolkit.execute_tool(
        "ergo_telegram_file", {"attachment_id": str(row.id)}
    )
    result = toolkit.execute_tool("ergo_telegram_delete", {})

    assert "Deleted message 102" in result
    assert plugin.api.calls[-1] == (
        "deleteMessage",
        {"chat_id": -123, "message_id": 102},
    )
    assert [m["message_id"] for m in plugin.sent_messages(session)] == [101]
    # The link Telegram's intake keeps is untouched.
    session.refresh_from_db()
    assert session.metadata["telegram"]["message_id"] == 7


@pytest.mark.django_db
def test_delete_by_id_and_refusals(settings, tmp_path):
    session, plugin, toolkit = _delete_setup(settings, tmp_path)

    assert "No message sent" in toolkit.execute_tool("ergo_telegram_delete", {})
    with pytest.raises(ValueError, match="not one of yours"):
        toolkit.execute_tool("ergo_telegram_delete", {"message_id": 7})
    # An id sent before ids were remembered can still be named.
    assert "Deleted message 55" in toolkit.execute_tool(
        "ergo_telegram_delete", {"message_id": 55}
    )
    assert plugin.api.calls == [("deleteMessage", {"chat_id": -123, "message_id": 55})]
