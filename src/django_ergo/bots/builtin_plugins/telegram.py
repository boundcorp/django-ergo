"""Telegram plugin: chat with a bot's root session over Telegram.

    plugins:
      - name: telegram
        token_env: KITCHEN_TELEGRAM_TOKEN   # bot token from @BotFather
        users:                               # Telegram chat id -> Django username
          123456789: lee

``bot.serve()`` long-polls Telegram. Each message from a listed chat goes to
that user's root session; messages from other chats are ignored. Photos,
voice notes, audio and documents arrive as attachments. A reply's
suggestions show as a one-time keyboard. When a turn stops
for approval, the reply carries Approve and Deny buttons, and pressing one
resumes the turn.

Other code can message a user with ``plugin.notify(user, text)``.
"""

from __future__ import annotations

import asyncio
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
        self._api: Any = None
        self._offset = 0

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

    async def user_for(self, chat_id):
        username = self.users.get(str(chat_id))
        if username is None:
            return None
        return await get_user_model().objects.filter(username=username).afirst()

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
        chat_id = message["chat"]["id"]
        user = await self.user_for(chat_id)
        if user is None:
            logger.info("Ignoring Telegram chat %s: not in users", chat_id)
            return
        text = message.get("text") or message.get("caption") or ""
        attachments = await self.attachments_for(message)
        if not text and not attachments:
            return
        session = await self.bot.root_session(user)
        result = await self.bot.ask(session, text, attachments=attachments or None)
        await self.send_result(chat_id, result)

    async def handle_callback(self, query: dict) -> None:
        await self.api.call("answerCallbackQuery", callback_query_id=query["id"])
        chat_id = query["message"]["chat"]["id"]
        user = await self.user_for(chat_id)
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

    async def serve(self) -> None:
        while True:
            try:
                await self.poll_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Telegram polling failed; retrying")
                await asyncio.sleep(5)
