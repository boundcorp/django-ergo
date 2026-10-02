"""Telegram plugin: chat with a bot's root session over Telegram.

    plugins:
      - name: telegram
        token_env: KITCHEN_TELEGRAM_TOKEN   # bot token from @BotFather
        users:                               # Telegram user or chat id -> username
          123456789: lee
        album_wait: 1.5                      # seconds to collect an album
        mode: auto                           # webhook, polling, or auto

``bot.serve()`` long-polls Telegram. A message's sender is looked up in
``users`` first, then its chat, so people in a shared group chat each talk
as themselves. Each message goes to that user's root session and the reply
goes back to the chat it came from; messages from anyone else are ignored.
Photos, voice notes, audio and documents arrive as attachments, and the
photos of an album arrive together as one message. A reply's
suggestions show as a one-time keyboard. When a turn stops
for approval, the reply carries Approve and Deny buttons, and pressing one
resumes the turn.

In ``auto`` mode the plugin uses a webhook when
``DJANGO_ERGO["BOT_WEBHOOK_BASE_URL"]`` is set (``bot.serve()`` registers it
with Telegram and returns), and long-polls otherwise. Webhook requests must
carry Telegram's secret-token header: the value of ``secret_env`` if set,
else one derived from the bot token.

Other code can message a user with ``plugin.notify(user, text)``.

Delegated work reaches Telegram too (``notify_delegations: true``, the
default): when a reply from another thread lands in the user's root chat,
the bot's answer to it is sent to their chat, and when a delegated request
in the root chat stops for approval, the Approve and Deny buttons are.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import urllib.request
from typing import TYPE_CHECKING
from typing import Any

from django.contrib.auth import get_user_model

from django_ergo.bots.plugins import BotPlugin
from django_ergo.conversation.attachments import Attachment

if TYPE_CHECKING:
    from django_ergo.bots.runtime import TurnResult

logger = logging.getLogger(__name__)

API_BASE = "https://api.telegram.org"
MAX_MESSAGE = 4096
REQUEST_TIMEOUT = 60


class TelegramAPI:
    """Minimal Bot API client (standard library only)."""

    def __init__(self, token: str, base: str = API_BASE):
        self.token = token
        self.base = base.rstrip("/")

    def _post(self, method: str, params: dict) -> Any:
        request = urllib.request.Request(  # noqa: S310 — fixed https base
            f"{self.base}/bot{self.token}/{method}",
            data=json.dumps(params).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:  # noqa: S310
            body = json.loads(response.read())
        if not body.get("ok"):
            msg = f"Telegram {method} failed: {body.get('description')}"
            raise RuntimeError(msg)
        return body["result"]

    def _get_file(self, file_id: str) -> bytes:
        info = self._post("getFile", {"file_id": file_id})
        url = f"{self.base}/file/bot{self.token}/{info['file_path']}"
        with urllib.request.urlopen(url, timeout=REQUEST_TIMEOUT) as response:  # noqa: S310
            return response.read()

    async def call(self, method: str, **params) -> Any:
        return await asyncio.to_thread(self._post, method, params)

    async def download(self, file_id: str) -> bytes:
        return await asyncio.to_thread(self._get_file, file_id)


class TelegramPlugin(BotPlugin):
    name = "telegram"

    def on_load(self) -> None:
        self.users = {str(k): v for k, v in (self.config.get("users") or {}).items()}
        self.poll_timeout = int(self.config.get("poll_timeout", 30))
        self.album_wait = float(self.config.get("album_wait", 1.5))
        self._albums: dict[tuple, list[dict]] = {}
        self._album_timers: dict[tuple, asyncio.Task] = {}
        self._tasks: set[asyncio.Task] = set()
        self.mode = str(self.config.get("mode", "auto"))
        if self.mode not in {"auto", "webhook", "polling"}:
            msg = f"Telegram mode must be auto, webhook or polling, not {self.mode!r}"
            raise ValueError(msg)
        self._api: Any = None
        self._offset = 0
        self.notify_delegations = bool(self.config.get("notify_delegations", True))

    @property
    def api(self):
        if self._api is None:
            env = self.config.get("token_env")
            token = os.environ.get(env, "") if env else ""
            if not token:
                msg = f"Telegram plugin needs a token in ${env or 'token_env'}"
                raise RuntimeError(msg)
            self._api = TelegramAPI(token, self.config.get("api_base", API_BASE))
        return self._api

    @api.setter
    def api(self, value) -> None:
        self._api = value

    # -- outbound ----------------------------------------------------------

    async def send_text(self, chat_id, text: str, **extra) -> None:
        text = text or "(no reply)"
        chunks = [text[i : i + MAX_MESSAGE] for i in range(0, len(text), MAX_MESSAGE)]
        for i, chunk in enumerate(chunks):
            params = extra if i == len(chunks) - 1 else {}
            await self.api.call("sendMessage", chat_id=chat_id, text=chunk, **params)

    def chat_for(self, user) -> str | None:
        for chat_id, username in self.users.items():
            if username == user.get_username():
                return chat_id
        return None

    async def notify(self, user, text: str) -> bool:
        """Message a user's chat, if they have one. Returns whether it sent."""
        chat_id = self.chat_for(user)
        if chat_id is None:
            return False
        await self.send_text(chat_id, text)
        return True

    async def after_turn(self, session, message: str, result: TurnResult) -> None:
        """Pass delegated work in a root chat on to the user's Telegram chat."""
        call = result.call
        message_id = (call.metadata or {}).get("thread_message") if call else None
        if not (self.notify_delegations and message_id and self.bot.is_root(session)):
            return
        from asgiref.sync import sync_to_async

        from django_ergo.conversation.models import ThreadMessage

        def lookup():
            found = (
                ThreadMessage.objects.filter(id=message_id)
                .values_list("in_reply_to_id", flat=True)
                .first()
            )
            return found, session.user

        in_reply_to, user = await sync_to_async(lookup)()
        is_reply = in_reply_to is not None
        if not is_reply and not result.approvals:
            return  # a request answered back to its sender: nothing for the user here
        chat_id = self.chat_for(user)
        if chat_id is None:
            return
        try:
            await self.send_result(chat_id, result)
        except Exception:
            logger.exception("Telegram notice for %s failed", session.id)

    async def send_result(self, chat_id, result: TurnResult) -> None:
        if not result.approvals:
            text = result.text or (
                f"Sorry, something went wrong: {result.error}" if result.error else ""
            )
            extra = {}
            if result.suggestions:
                # Suggested replies become a one-time keyboard; typing still works.
                extra["reply_markup"] = {
                    "keyboard": [[{"text": s}] for s in result.suggestions],
                    "one_time_keyboard": True,
                    "resize_keyboard": True,
                }
            await self.send_text(chat_id, text, **extra)
            return
        names = ", ".join(a.tool_name for a in result.approvals)
        text = (result.text + "\n\n" if result.text else "") + f"Approve {names}?"
        session_id = result.session.id
        await self.send_text(
            chat_id,
            text,
            reply_markup={
                "inline_keyboard": [
                    [
                        {"text": "Approve", "callback_data": f"ok:{session_id}"},
                        {"text": "Deny", "callback_data": f"no:{session_id}"},
                    ]
                ]
            },
        )

    # -- inbound -----------------------------------------------------------

    async def user_for(self, *ids):
        """The user for the first listed id (sender, then chat)."""
        for telegram_id in ids:
            if telegram_id is None:
                continue
            username = self.users.get(str(telegram_id))
            if username is not None:
                return await get_user_model().objects.filter(username=username).afirst()
        return None

    async def attachments_for(self, message: dict) -> list[Attachment]:
        found: list[tuple[str, str, str]] = []  # (file_id, media_type, filename)
        if photos := message.get("photo"):
            found.append((photos[-1]["file_id"], "image/jpeg", "photo.jpg"))
        for key, default_type, default_name in (
            ("voice", "audio/ogg", "voice.ogg"),
            ("audio", "audio/mpeg", "audio.mp3"),
            ("document", "application/octet-stream", "file"),
        ):
            if item := message.get(key):
                found.append(
                    (
                        item["file_id"],
                        item.get("mime_type", default_type),
                        item.get("file_name", default_name),
                    )
                )
        return [
            Attachment(
                media_type=media_type,
                data=await self.api.download(file_id),
                filename=filename,
                metadata={"telegram_file_id": file_id},
            )
            for file_id, media_type, filename in found
        ]

    async def handle_message(self, message: dict) -> None:
        if group_id := message.get("media_group_id"):
            self._queue_album(message, group_id)
            return
        await self.handle_messages([message])

    def _queue_album(self, message: dict, group_id: str) -> None:
        key = (message["chat"]["id"], group_id)
        self._albums.setdefault(key, []).append(message)
        if timer := self._album_timers.get(key):
            timer.cancel()
        self._album_timers[key] = asyncio.create_task(self._flush_album_later(key))

    async def _flush_album_later(self, key: tuple) -> None:
        await asyncio.sleep(self.album_wait)
        self._album_timers.pop(key, None)
        await self._flush_album(key)

    async def _flush_album(self, key: tuple) -> None:
        messages = self._albums.pop(key, [])
        if not messages:
            return
        try:
            await self.handle_messages(messages)
        except Exception:
            logger.exception("Telegram album %s failed", key[1])

    async def flush_albums(self) -> None:
        """Handle every album still being collected, now."""
        for key in list(self._albums):
            if timer := self._album_timers.pop(key, None):
                timer.cancel()
            await self._flush_album(key)

    async def handle_messages(self, messages: list[dict]) -> None:
        """Answer one or more messages (an album) as a single turn."""
        first = messages[0]
        chat_id = first["chat"]["id"]
        user = await self.user_for((first.get("from") or {}).get("id"), chat_id)
        if user is None:
            logger.info("Ignoring Telegram chat %s: not in users", chat_id)
            return
        texts = [m.get("text") or m.get("caption") or "" for m in messages]
        text = "\n\n".join(t for t in texts if t)
        attachments = [a for m in messages for a in await self.attachments_for(m)]
        if not text and not attachments:
            return
        session = await self.bot.root_session(user)
        result = await self.bot.ask(session, text, attachments=attachments or None)
        await self.send_result(chat_id, result)

    async def handle_callback(self, query: dict) -> None:
        await self.api.call("answerCallbackQuery", callback_query_id=query["id"])
        chat_id = query["message"]["chat"]["id"]
        user = await self.user_for((query.get("from") or {}).get("id"), chat_id)
        action, _, session_id = (query.get("data") or "").partition(":")
        if user is None or action not in {"ok", "no"}:
            return
        session = await (
            self.bot.sessions(user)
            .filter(id=session_id)
            .select_related("user")
            .afirst()
        )
        if session is None:
            return
        if await self.bot.pending_call(session) is None:
            await self.send_text(chat_id, "That request was already handled.")
            return
        result = await self.bot.resume(session, action == "ok")
        await self.send_result(chat_id, result)

    async def handle_update(self, update: dict) -> None:
        self._offset = max(self._offset, update["update_id"] + 1)
        try:
            if "callback_query" in update:
                await self.handle_callback(update["callback_query"])
            elif message := update.get("message"):
                await self.handle_message(message)
        except Exception:
            logger.exception("Telegram update %s failed", update.get("update_id"))

    async def poll_once(self) -> int:
        updates = await self.api.call(
            "getUpdates",
            offset=self._offset,
            timeout=self.poll_timeout,
            allowed_updates=["message", "callback_query"],
        )
        for update in updates:
            await self.handle_update(update)
        return len(updates)

    # -- webhook ---------------------------------------------------------

    @property
    def webhook_secret(self) -> str:
        env = self.config.get("secret_env")
        if env and os.environ.get(env):
            return os.environ[env]
        return hashlib.sha256(f"ergo-telegram:{self.api.token}".encode()).hexdigest()[
            :48
        ]

    @property
    def uses_webhook(self) -> bool:
        if self.mode == "polling":
            return False
        url = self.webhook_url("update")
        if self.mode == "webhook" and not url:
            msg = "Telegram webhook mode needs DJANGO_ERGO['BOT_WEBHOOK_BASE_URL']"
            raise RuntimeError(msg)
        return bool(url)

    def webhooks(self):
        return {"update": self.receive_webhook}

    async def receive_webhook(self, request):
        from django.http import HttpResponse

        given = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if not hmac.compare_digest(given, self.webhook_secret):
            return HttpResponse(status=403)
        try:
            update = json.loads(request.body or b"{}")
        except ValueError:
            return HttpResponse(status=400)
        # Answer Telegram at once; the turn runs after the response.
        task = asyncio.create_task(self.handle_update(update))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return None

    async def drain(self) -> None:
        """Wait for webhook updates still being handled."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks))

    async def register_webhook(self) -> None:
        await self.api.call(
            "setWebhook",
            url=self.webhook_url("update"),
            secret_token=self.webhook_secret,
            allowed_updates=["message", "callback_query"],
        )

    async def serve(self) -> None:
        if self.uses_webhook:
            await self.register_webhook()
            return
        await self.api.call("deleteWebhook")
        while True:
            try:
                await self.poll_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Telegram polling failed; retrying")
                await asyncio.sleep(5)
