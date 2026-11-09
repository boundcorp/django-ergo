"""Inbound Telegram files are stored on the shared conversation, not just described."""

import pytest
from asgiref.sync import sync_to_async

from django_ergo.conversation.models import ConversationAttachment
from django_ergo.plugins import telegram
from django_ergo.settings import api_settings
from tests.test_bot_plugins import User
from tests.test_bot_plugins import group_update
from tests.test_bot_plugins import passive_telegram_bot
from tests.test_bots import say


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    ("field", "item", "media_type", "filename", "file_id"),
    [
        (
            "photo",
            [{"file_id": "small"}, {"file_id": "large"}],
            "image/jpeg",
            "photo.jpg",
            "large",
        ),
        (
            "video",
            {"file_id": "vid", "file_name": "clip.mp4"},
            "video/mp4",
            "clip.mp4",
            "vid",
        ),
        (
            "document",
            {"file_id": "doc", "mime_type": "application/pdf", "file_name": "a.pdf"},
            "application/pdf",
            "a.pdf",
            "doc",
        ),
        (
            "document",
            {"file_id": "path", "file_name": "../dir\\safe.pdf"},
            "application/octet-stream",
            "safe.pdf",
            "path",
        ),
        (
            "audio",
            {"file_id": "sound", "mime_type": "audio/mpeg"},
            "audio/mpeg",
            "audio.mp3",
            "sound",
        ),
        ("voice", {"file_id": "voice"}, "audio/ogg", "voice.ogg", "voice"),
    ],
)
async def test_passive_files_are_persisted_without_inference(  # noqa: PLR0913
    tmp_path, settings, monkeypatch, field, item, media_type, filename, file_id
):
    settings.MEDIA_ROOT = tmp_path / "media"
    await User.objects.acreate(username="cook")
    bot, engine, plugin = passive_telegram_bot(tmp_path)

    async def forbidden(*args, **kwargs):
        pytest.fail(
            "passive capture must not call the model, transcriber or turn runner"
        )

    monkeypatch.setattr(api_settings, "AUDIO_TRANSCRIBER", forbidden)
    monkeypatch.setattr(api_settings, "CHANNEL_TURN_RUNNER", forbidden)
    await plugin.handle_update(group_update(1, caption="context", **{field: item}))
    session = await bot.sessions().aget(metadata__telegram_thread="Telegram")
    message = await session.messages.aget()
    attachment = await ConversationAttachment.objects.aget(
        session=session, message_sequence=message.sequence
    )
    assert attachment.media_type == media_type
    assert attachment.filename == filename
    assert attachment.metadata == {"telegram_file_id": file_id}
    assert attachment.transcript == ""
    assert (
        await sync_to_async(attachment.file.read)()
        == b"\x89PNG fake " + file_id.encode()
    )
    assert not engine._client.calls
    assert not plugin.api.sent()


@pytest.mark.django_db(transaction=True)
async def test_active_album_persists_each_attachment_in_one_turn(tmp_path, settings):
    settings.MEDIA_ROOT = tmp_path / "media"
    await User.objects.acreate(username="cook")
    bot, engine, plugin = passive_telegram_bot(tmp_path, say("Received."))
    messages = [
        group_update(1, sender_id=111, caption="album", photo=[{"file_id": "one"}])[
            "message"
        ],
        group_update(2, sender_id=111, video={"file_id": "two"})["message"],
    ]
    await plugin.handle_messages(messages)
    session = await bot.sessions().aget(metadata__telegram_thread="Telegram")
    rows = [
        row
        async for row in ConversationAttachment.objects.filter(
            session=session
        ).order_by("position")
    ]
    assert [row.metadata["telegram_file_id"] for row in rows] == ["one", "two"]
    assert rows[0].message_sequence == rows[1].message_sequence
    assert len(engine._client.calls) == 1


@pytest.mark.django_db(transaction=True)
async def test_passive_album_replay_preserves_each_message_once(tmp_path, settings):
    settings.MEDIA_ROOT = tmp_path / "media"
    await User.objects.acreate(username="cook")
    bot, engine, plugin = passive_telegram_bot(tmp_path)
    for number in (1, 2, 1):
        await plugin.handle_update(
            group_update(
                number, media_group_id="album", photo=[{"file_id": str(number)}]
            )
        )
    session = await bot.sessions().aget(metadata__telegram_thread="Telegram")
    assert await session.messages.acount() == 2
    rows = [row async for row in ConversationAttachment.objects.filter(session=session)]
    assert {row.metadata["telegram_file_id"] for row in rows} == {"1", "2"}
    assert len({row.message_sequence for row in rows}) == 2
    assert not engine._client.calls


@pytest.mark.django_db(transaction=True)
async def test_oversized_passive_file_is_not_downloaded_and_is_recorded(
    tmp_path, monkeypatch
):
    await User.objects.acreate(username="cook")
    bot, engine, plugin = passive_telegram_bot(tmp_path)

    async def forbidden(file_id):
        pytest.fail("oversized file must not be downloaded")

    monkeypatch.setattr(plugin.api, "download", forbidden)
    await plugin.handle_update(
        group_update(
            1, document={"file_id": "big", "file_size": telegram.MAX_DOWNLOAD_BYTES + 1}
        )
    )
    session = await bot.sessions().aget(metadata__telegram_thread="Telegram")
    message = await session.messages.aget()
    block = await message.content_blocks.aget()
    assert "not downloaded" in block.text
    assert not await ConversationAttachment.objects.filter(session=session).aexists()
    assert not engine._client.calls


def test_download_bounds_untrusted_response(monkeypatch):
    monkeypatch.setattr(telegram, "MAX_DOWNLOAD_BYTES", 3)
    api = telegram.TelegramAPI("fake-token")
    monkeypatch.setattr(api, "_post", lambda *args: {"file_path": "file"})

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def read(self, size):
            assert size == 4
            return b"1234"

    monkeypatch.setattr(
        telegram.urllib.request, "urlopen", lambda *args, **kwargs: Response()
    )
    with pytest.raises(ValueError, match="download limit"):
        api._get_file("id")


@pytest.mark.django_db(transaction=True)
async def test_unlisted_group_does_not_download_or_start_turn(tmp_path, monkeypatch):
    await User.objects.acreate(username="cook")
    bot, engine, plugin = passive_telegram_bot(tmp_path)

    async def forbidden(file_id):
        pytest.fail("unlisted group must not download media")

    monkeypatch.setattr(plugin.api, "download", forbidden)
    await plugin.handle_update(group_update(1, chat_id=-999, video={"file_id": "vid"}))
    assert not await bot.sessions().aexists()
    assert not engine._client.calls


def test_download_rejects_oversized_getfile_metadata(monkeypatch):
    api = telegram.TelegramAPI("fake-token")
    monkeypatch.setattr(
        api,
        "_post",
        lambda *args: {
            "file_path": "file",
            "file_size": telegram.MAX_DOWNLOAD_BYTES + 1,
        },
    )

    def forbidden(*args, **kwargs):
        pytest.fail("oversized getFile response must not be fetched")

    monkeypatch.setattr(telegram.urllib.request, "urlopen", forbidden)
    with pytest.raises(ValueError, match="download limit"):
        api._get_file("id")
