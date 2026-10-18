"""Telegram plugin: chat with a bot over Telegram.

    plugins:
      - name: telegram
        token_env: KITCHEN_TELEGRAM_TOKEN   # bot token from @BotFather
        users:                               # Telegram user or chat id -> username
          123456789: lee
        respond_to:
          users: all            # all, or Telegram ids and @usernames (default: those in users)
          messages: all         # all, private (1:1 chats only), or mentions (groups:
                                # only @mentions of the bot and replies to it)
        target_thread: Telegram # every chat goes into one shared thread with this title
        owner: lee              # who owns that thread (default: the first admin)
        replies: auto           # auto: each answer goes back to the chat; tool: the bot
                                # sends with ergo_telegram_message when it has something to say
        photo_batch_wait: 0     # seconds to gather photos sent one by one into one turn
        album_wait: 1.5         # seconds to collect an album
        mode: auto              # webhook, polling, or auto

Without ``target_thread`` each person talks to their own main chat: a
message's sender is looked up in ``users`` first, then its chat, so people
in a shared group chat each talk as themselves, and anyone not listed is
ignored. With ``target_thread`` every message, from whoever
``respond_to.users`` allows, goes into one thread under the owner's main
chat (``respond_to: {users: all}`` needs it, since unknown people have no
chat of their own). Either way the message's author is the Telegram user,
and the plugin keeps a profile for each one (name, username and profile
picture, as ``AuthorProfile`` rows) for apps to show.

Photos, voice notes, audio and documents arrive as attachments (voice is
transcribed when ``DJANGO_ERGO["AUDIO_TRANSCRIBER"]`` is set), and the
photos of an album arrive together as one message. With
``photo_batch_wait`` set, photos a person sends one at a time are gathered
the same way, and a text they send while photos are gathering goes with
them. A reply's suggestions show as a one-time keyboard. When a turn stops
for approval, the reply carries Approve and Deny buttons, and pressing one
resumes the turn (``replies: tool`` too).

With ``replies: tool`` a turn's answer stays in the chat app; the bot
writes to Telegram with ``ergo_telegram_message`` (the plugin's skill, loaded
in the chats Telegram talks to). Turns run through
``DJANGO_ERGO["CHANNEL_TURN_RUNNER"]`` when it is set (Ergonaut holds the
session's turn lock), else one at a time per session in this process.

In ``auto`` mode the plugin uses a webhook when
``DJANGO_ERGO["BOT_WEBHOOK_BASE_URL"]`` is set (``bot.serve()`` registers it
with Telegram and returns), and long-polls otherwise. Webhook requests must
carry Telegram's secret-token header: the value of ``secret_env`` if set,
else one derived from the bot token.

Other code can message a user with ``plugin.notify(user, text)``.

Delegated work reaches Telegram too (``notify_delegations: true``, the
default, main chats only): when a reply from another thread lands in the
user's main chat, the bot's answer to it is sent to their chat, as is the
answer to a scheduled message (see bots.schedules), and when a delegated
request in the main chat stops for approval, the Approve and Deny buttons are.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import json
import logging
import os
import time
import urllib.request
from datetime import timedelta
from typing import TYPE_CHECKING
from typing import Any

from django.contrib.auth import get_user_model
from django.db import DatabaseError

from django_ergo.bots.plugins import BotPlugin
from django_ergo.conversation.attachments import Attachment

if TYPE_CHECKING:
    from django_ergo.bots.runtime import TurnResult
    from django_ergo.bots.tools import ToolContext
    from django_ergo.conversation.toolkit import Toolkit

logger = logging.getLogger(__name__)

API_BASE = "https://api.telegram.org"
MAX_MESSAGE = 4096
REQUEST_TIMEOUT = 60
MAX_APPROVAL_PREVIEW_CHARS = 3_000
AVATAR_SIDE = 160  # pixels: the profile photo size kept for chat avatars
AVATAR_REFRESH = timedelta(days=1)
JOB_TARGET = "plugin:telegram"  # BotJob.target for saved updates
REPLAY_AFTER = timedelta(minutes=2)  # a pending update this old was never started
REPLAY_EVERY = 60  # seconds between looks for updates to replay
KEEP_HANDLED = timedelta(days=7)  # handled updates are deleted after this
MAX_ATTEMPTS = 3  # tries for an update the database kept failing


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
    description = "Send messages to the Telegram chat this conversation is with"

    def on_load(self) -> None:
        self.users = {str(k): v for k, v in (self.config.get("users") or {}).items()}
        self.poll_timeout = int(self.config.get("poll_timeout", 30))
        self.album_wait = float(self.config.get("album_wait", 1.5))
        self.photo_batch_wait = float(self.config.get("photo_batch_wait", 0) or 0)
        self._albums: dict[tuple, list[dict]] = {}
        self._album_timers: dict[tuple, asyncio.Task] = {}
        self._tasks: set[asyncio.Task] = set()
        self._replaying = False
        self._replayed_at = float("-inf")
        self._locks: dict[str, asyncio.Lock] = {}
        self.mode = str(self.config.get("mode", "auto"))
        if self.mode not in {"auto", "webhook", "polling"}:
            msg = f"Telegram mode must be auto, webhook or polling, not {self.mode!r}"
            raise ValueError(msg)
        self.target_thread = str(self.config.get("target_thread") or "")
        self.owner = str(self.config.get("owner") or "")
        self.replies = str(self.config.get("replies", "auto"))
        if self.replies not in {"auto", "tool"}:
            msg = f"Telegram replies must be auto or tool, not {self.replies!r}"
            raise ValueError(msg)
        respond_to = self.config.get("respond_to") or {}
        if not isinstance(respond_to, dict):
            msg = "Telegram respond_to must be a mapping of users and messages"
            raise ValueError(msg)  # noqa: TRY004
        allowed = respond_to.get("users")
        self.all_users = allowed == "all"
        self.allowed_users = (
            set()
            if allowed in (None, "all")
            else {
                str(u).lower()
                for u in (allowed if isinstance(allowed, list) else [allowed])
            }
        )
        if self.all_users and not self.target_thread:
            msg = "Telegram respond_to users: all needs a target_thread"
            raise ValueError(msg)
        self.messages = str(respond_to.get("messages", "all"))
        if self.messages not in {"all", "private", "mentions"}:
            msg = f"Telegram respond_to messages must be all, private or mentions, not {self.messages!r}"
            raise ValueError(msg)
        self._api: Any = None
        self._offset = 0
        self._username: str | None = None
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

    @property
    def bot_id(self) -> str:
        """The bot's own Telegram id: the part of its token before the colon."""
        return str(getattr(self.api, "token", "") or "").partition(":")[0]

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
        """Pass delegated work in a main chat on to the user's Telegram chat."""
        call = result.call
        message_id = (call.metadata or {}).get("thread_message") if call else None
        if not (self.notify_delegations and message_id and self.bot.is_root(session)):
            return
        from asgiref.sync import sync_to_async

        from django_ergo.conversation.models import ThreadMessage

        def lookup():
            found = (
                ThreadMessage.objects.filter(id=message_id)
                .values("in_reply_to_id", "metadata")
                .first()
            )
            return found or {}, session.user

        found, user = await sync_to_async(lookup)()
        is_reply = found.get("in_reply_to_id") is not None
        scheduled = bool((found.get("metadata") or {}).get("schedule"))
        if not (is_reply or scheduled or result.approvals):
            return  # a request answered back to its sender: nothing for the user here
        chat_id = self.chat_for(user)
        if chat_id is None:
            return
        try:
            await self.send_result(chat_id, result)
        except Exception:
            logger.exception("Telegram notice for %s failed", session.id)

    async def deliver(self, chat_id, result: TurnResult) -> None:
        """A Telegram message's turn, back to its chat: the answer when replies
        are auto, else only approval buttons and errors (the bot sends the rest)."""
        if self.replies == "auto" or result.approvals:
            await self.send_result(chat_id, result)
        elif result.error:
            await self.send_text(
                chat_id, f"Sorry, something went wrong: {result.error}"
            )

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
        previews = "\n\n".join(
            f"{approval.tool_name} preview:\n{approval.preview}"
            for approval in result.approvals
            if approval.preview
        )
        if len(previews) > MAX_APPROVAL_PREVIEW_CHARS:
            previews = (
                previews[:MAX_APPROVAL_PREVIEW_CHARS]
                + "\n[... preview truncated for Telegram ...]"
            )
        text = (result.text + "\n\n" if result.text else "") + f"Approve {names}?"
        if previews:
            text += f"\n\n{previews}"
        # The buttons name the waiting call and what it waits on, so an old
        # prompt can't approve a newer one (even in the same call).
        call_id = result.call.id if result.call else ""
        stamp = approvals_stamp([a.tool_use_id for a in result.approvals])
        await self.send_text(
            chat_id,
            text,
            reply_markup={
                "inline_keyboard": [
                    [
                        {"text": "Approve", "callback_data": f"ok:{call_id}:{stamp}"},
                        {"text": "Deny", "callback_data": f"no:{call_id}:{stamp}"},
                    ]
                ]
            },
        )

    # -- the bot's tool ------------------------------------------------------

    def toolkits(self, ctx: ToolContext) -> list[Toolkit]:
        from django_ergo.bots.tools import FunctionToolkit

        return [FunctionToolkit(self._tools(), ctx)]

    @property
    def skill_instructions(self) -> str:
        if self.replies == "tool":
            how = (
                "Your answers are not sent to Telegram on their own: to say something "
                "there, call ergo_telegram_message. Answer each Telegram message that "
                "asks or tells you something with it, keep it short (it is a chat), "
                "and don't send the same thing twice."
            )
        else:
            how = (
                "Your answer to a Telegram message is sent back to its chat. Use "
                "ergo_telegram_message only for something extra, such as a second "
                "message or a note from a turn Telegram didn't start."
            )
        return (
            "Messages from Telegram arrive with their author (a Telegram user). "
            + how
            + " Messages go to the chat the latest Telegram message came from."
        )

    def _tools(self) -> list:
        from asgiref.sync import async_to_sync

        from django_ergo.bots.tools import bot_tool

        plugin = self

        @bot_tool(
            name="ergo_telegram_message",
            takes_context=True,
            description=(
                "Send a message to the Telegram chat this conversation is with (the "
                "chat of the latest Telegram message). Plain text."
            ),
            parameters={
                "text": {"type": "string", "description": "What to say"},
                "reply": {
                    "type": "boolean",
                    "description": "Quote the latest Telegram message (default false)",
                },
            },
            required=["text"],
        )
        def send(ctx: ToolContext, text: str, reply: bool = False) -> str:
            link = plugin.chat_link(ctx.session)
            if not link.get("chat_id"):
                return "No Telegram chat has written to this conversation yet."
            extra = {}
            if reply and link.get("message_id"):
                extra["reply_parameters"] = {
                    "message_id": link["message_id"],
                    "allow_sending_without_reply": True,
                }
            async_to_sync(plugin.send_text)(link["chat_id"], text, **extra)
            return f"Sent to Telegram ({link.get('title') or link['chat_id']})."

        return [send.__bot_tool__]

    @staticmethod
    def chat_link(session) -> dict:
        """The Telegram chat a session last heard from (fresh from the database)."""
        from django_ergo.conversation.models import ConversationSession

        if session is None or session.pk is None:
            return {}
        metadata = (
            ConversationSession.objects.filter(pk=session.pk)
            .values_list("metadata", flat=True)
            .first()
        )
        return (metadata or {}).get("telegram") or {}

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

    def allowed(self, sender: dict, chat: dict | None = None) -> bool:
        """Whether ``respond_to.users`` lets this sender (or their chat) talk to the bot."""
        if self.all_users:
            return True
        ids = [sender.get("id"), (chat or {}).get("id")]
        if self.allowed_users:
            names = {str(i) for i in ids if i is not None}
            if sender.get("username"):
                names |= {sender["username"].lower(), f"@{sender['username'].lower()}"}
            return bool(names & self.allowed_users)
        return any(str(i) in self.users for i in ids if i is not None)

    async def bot_username(self) -> str:
        if self._username is None:
            try:
                me = await self.api.call("getMe")
                self._username = (
                    str((me or {}).get("username") or "")
                    if isinstance(me, dict)
                    else ""
                )
            except Exception:
                logger.exception("Telegram getMe failed")
                return ""
        return self._username

    async def wanted(self, message: dict) -> bool:
        """Whether ``respond_to.messages`` answers this message."""
        private = (message.get("chat") or {}).get("type", "private") == "private"
        if self.messages == "all" or private:
            return True
        if self.messages == "private":
            return False
        replied = (message.get("reply_to_message") or {}).get("from") or {}
        if self.bot_id and str(replied.get("id")) == self.bot_id:
            return True
        username = await self.bot_username()
        text = message.get("text") or message.get("caption") or ""
        return bool(username) and f"@{username.lower()}" in text.lower()

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
        sender_id = (message.get("from") or {}).get("id")
        photos_key = (chat_id, "photos", sender_id)
        if group_id := message.get("media_group_id"):
            self._queue((chat_id, group_id), message, self.album_wait)
        elif self.photo_batch_wait and message.get("photo"):
            self._queue(photos_key, message, self.photo_batch_wait)
        elif photos_key in self._albums:
            # Words sent after the photos go with them, now.
            self._albums[photos_key].append(message)
            if timer := self._album_timers.pop(photos_key, None):
                timer.cancel()
            await self._flush_album(photos_key)
        else:
            await self.handle_messages([message])

    def _queue(self, key: tuple, message: dict, wait: float) -> None:
        self._albums.setdefault(key, []).append(message)
        if timer := self._album_timers.get(key):
            timer.cancel()
        self._album_timers[key] = asyncio.create_task(
            self._flush_album_later(key, wait)
        )

    async def _flush_album_later(self, key: tuple, wait: float) -> None:
        await asyncio.sleep(wait)
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

    async def session_for(self, sender: dict, chat: dict):
        """The chat a message goes to: the shared thread, or the person's main chat."""
        if not self.target_thread:
            user = await self.user_for(sender.get("id"), chat.get("id"))
            return None if user is None else await self.bot.root_session(user)
        from asgiref.sync import sync_to_async

        from django_ergo.bots import archival

        found = (
            await self.bot.sessions()
            .filter(metadata__telegram_thread=self.target_thread)
            .select_related("user")
            .order_by("created_at")
            .afirst()
        )
        if found is not None:
            await sync_to_async(archival.reopen)(found)
            return found
        owner = await self.thread_owner()
        if owner is None:
            logger.warning(
                "Telegram thread %r has no owner to belong to", self.target_thread
            )
            return None
        main = self.bot.definition.chats.get("main")
        skills = [*(main.skills if main else []), self.skill_name]
        return await self.bot.create_session(
            owner,
            parent=await self.bot.root_session(owner),
            title=self.target_thread,
            metadata={"telegram_thread": self.target_thread, "always_skills": skills},
        )

    async def thread_owner(self):
        """``owner`` (a username or email), else the first admin, else the first listed user."""
        from django.db.models import Q

        users = get_user_model().objects.filter(is_active=True)
        if self.owner:
            return await users.filter(
                Q(username=self.owner) | Q(email=self.owner)
            ).afirst()
        admin = await users.filter(is_superuser=True).order_by("pk").afirst()
        if admin is not None:
            return admin
        return (
            await users.filter(username__in=list(self.users.values()))
            .order_by("pk")
            .afirst()
        )

    async def link_chat(self, session, message: dict) -> None:
        """Remember the chat (and message) a session last heard from, for the tool."""
        from asgiref.sync import sync_to_async

        from django_ergo.conversation.models import ConversationSession

        chat = message.get("chat") or {}
        link = {
            "chat_id": chat.get("id"),
            "message_id": message.get("message_id"),
            "title": chat.get("title")
            or chat.get("first_name")
            or chat.get("username")
            or "",
        }
        skill = self.skill_name

        def save():
            fresh = (
                ConversationSession.objects.filter(pk=session.pk)
                .values_list("metadata", flat=True)
                .first()
            ) or {}
            skills = list(fresh.get("always_skills") or [])
            if self.replies == "tool" and skill not in skills:
                skills.append(skill)
            metadata = {
                **fresh,
                "telegram": link,
                **({"always_skills": skills} if skills else {}),
            }
            ConversationSession.objects.filter(pk=session.pk).update(metadata=metadata)
            session.metadata = metadata

        await sync_to_async(save)()

    @staticmethod
    def author_for(sender: dict) -> dict | None:
        if sender.get("id") is None:
            return None
        name = " ".join(
            str(sender[key]) for key in ("first_name", "last_name") if sender.get(key)
        )
        return {
            "kind": "telegram_user",
            "ref": str(sender["id"]),
            "display_name": name or sender.get("username") or str(sender["id"]),
        }

    async def track(self, sender: dict) -> None:
        """Keep the sender's profile: name, username and (daily) profile picture."""
        author = self.author_for(sender)
        if author is None:
            return
        from django.utils import timezone

        from django_ergo.conversation.models import AuthorProfile

        now = timezone.now()
        profile, _ = await AuthorProfile.objects.aget_or_create(
            kind=author["kind"], ref=author["ref"]
        )
        profile.display_name = author["display_name"]
        profile.username = str(sender.get("username") or "")
        profile.last_seen_at = now
        if (
            profile.avatar_checked_at is None
            or now - profile.avatar_checked_at > AVATAR_REFRESH
        ):
            profile.avatar_checked_at = now
            try:
                await self._fetch_avatar(profile, sender["id"])
            except Exception:
                logger.exception("Telegram profile photo for %s failed", sender["id"])
        await profile.asave()

    async def _fetch_avatar(self, profile, user_id) -> None:
        photos = await self.api.call("getUserProfilePhotos", user_id=user_id, limit=1)
        sizes = (
            ((photos or {}).get("photos") or [[]])[0]
            if isinstance(photos, dict)
            else []
        )
        if not sizes:
            profile.avatar, profile.avatar_type, profile.avatar_source = None, "", ""
            return
        # The smallest size that is still sharp as a chat avatar (sizes go up).
        size = next((s for s in sizes if s.get("width", 0) >= AVATAR_SIDE), sizes[-1])
        source = size.get("file_unique_id") or size["file_id"]
        if source == profile.avatar_source and profile.avatar:
            return
        profile.avatar = await self.api.download(size["file_id"])
        profile.avatar_type = "image/jpeg"
        profile.avatar_source = source

    async def run_turn(self, session, run):
        """Run a turn the host's way (its turn lock), else one at a time per session."""
        from django_ergo.settings import api_settings

        runner = api_settings.CHANNEL_TURN_RUNNER
        if runner is not None:
            return await runner(session, run)
        lock = self._locks.setdefault(str(session.id), asyncio.Lock())
        async with lock:
            return await run(None)

    async def handle_messages(self, messages: list[dict]) -> None:
        """Answer one or more messages (an album) as a single turn."""
        first = messages[0]
        chat = first["chat"]
        chat_id = chat["id"]
        sender = first.get("from") or {}
        if not self.allowed(sender, chat):
            logger.info("Ignoring Telegram chat %s: not in respond_to users", chat_id)
            return
        if not any([await self.wanted(m) for m in messages]):
            return
        texts = [m.get("text") or m.get("caption") or "" for m in messages]
        text = "\n\n".join(t for t in texts if t)
        attachments = [a for m in messages for a in await self.attachments_for(m)]
        if not text and not attachments:
            return
        session = await self.session_for(sender, chat)
        if session is None:
            logger.info("Ignoring Telegram chat %s: no chat for it", chat_id)
            return
        await self.track(sender)
        await self.link_chat(session, messages[-1])
        author = self.author_for(sender)

        async def run(control):
            if await self.bot.pending_call(session) is not None:
                # A new message instead of Approve/Deny: decline what was waiting.
                await self.bot.resume(session, decisions=False)
            return await self.bot.ask(
                session,
                text,
                attachments=attachments or None,
                author=author,
                control=control,
            )

        result = await self.run_turn(session, run)
        await self.deliver(chat_id, result)

    async def handle_callback(self, query: dict) -> None:
        from django_ergo.conversation.models import StructuredCall

        await self.api.call("answerCallbackQuery", callback_query_id=query["id"])
        chat_id = query["message"]["chat"]["id"]
        sender = query.get("from") or {}
        action, _, rest = (query.get("data") or "").partition(":")
        call_id, _, stamp = rest.partition(":")
        if action not in {"ok", "no"} or not _is_uuid(call_id):
            return
        calls = StructuredCall.objects.filter(
            id=call_id, session__bot_name=self.bot.name
        )
        if self.target_thread:
            # Anyone the thread answers may answer its approvals.
            if not self.allowed(sender):
                return
            calls = calls.filter(session__metadata__telegram_thread=self.target_thread)
        else:
            # Only the person themselves can answer, never anyone else in a group chat.
            user = await self.user_for(sender.get("id"))
            if user is None:
                return
            calls = calls.filter(session__user=user)
        call = await calls.select_related("session__user").afirst()
        if call is None:
            return
        session = call.session

        async def run(control):
            pending = await self.bot.pending_call(session)
            waiting = (
                [a["id"] for a in (pending.metadata or {}).get("pending_approvals", [])]
                if pending
                else []
            )
            if (
                pending is None
                or pending.id != call.id
                or approvals_stamp(waiting) != stamp
            ):
                return None
            return await self.bot.resume(session, action == "ok", control=control)

        result = await self.run_turn(session, run)
        if result is None or result.call is None:
            await self.send_text(chat_id, "That request was already handled.")
            return
        await self.deliver(chat_id, result)

    async def dispatch(self, update: dict) -> None:
        if "callback_query" in update:
            await self.handle_callback(update["callback_query"])
        elif message := update.get("message"):
            await self.handle_message(message)

    async def handle_update(self, update: dict) -> None:
        self._offset = max(self._offset, update["update_id"] + 1)
        try:
            await self.dispatch(update)
        except Exception:
            logger.exception("Telegram update %s failed", update.get("update_id"))

    # -- durable intake ----------------------------------------------------
    # Each update is saved as a BotJob before Telegram hears it arrived, so an
    # update that came in while the database could not write is redelivered
    # by Telegram, and one that was saved but never handled is replayed.

    async def accept(self, update: dict):
        """Save an update as a pending BotJob; None if it was saved already."""
        from django_ergo.conversation.models import BotJob

        name = f"telegram update {update['update_id']}"
        jobs = BotJob.objects.filter(bot_name=self.bot.name, target=JOB_TARGET)
        if await jobs.filter(name=name).aexists():
            return None
        return await BotJob.objects.acreate(
            bot_name=self.bot.name,
            name=name,
            target=JOB_TARGET,
            args={"update": update},
        )

    async def process(self, job) -> None:
        """Handle a saved update once: claim it, run it, record how it went."""
        import traceback

        from django.utils import timezone

        from django_ergo.conversation.models import BotJob

        claimed = await BotJob.objects.filter(pk=job.pk, status="pending").aupdate(
            status="in_progress", started_at=timezone.now()
        )
        if not claimed:
            return
        update = job.args["update"]
        try:
            await self.dispatch(update)
        except Exception as error:
            logger.exception("Telegram update %s failed", update.get("update_id"))
            await self._reset_connections()
            attempts = int(job.args.get("attempts", 0)) + 1
            if isinstance(error, DatabaseError) and attempts < MAX_ATTEMPTS:
                # The database failed, not the message: replay it later.
                with contextlib.suppress(Exception):
                    await BotJob.objects.filter(pk=job.pk).aupdate(
                        status="pending", args={**job.args, "attempts": attempts}
                    )
                return
            await self._failed(update, error)
            with contextlib.suppress(Exception):
                await BotJob.objects.filter(pk=job.pk).aupdate(
                    status="failed",
                    error=str(error)[:2000],
                    traceback=traceback.format_exc(),
                    completed_at=timezone.now(),
                )
            return
        await BotJob.objects.filter(pk=job.pk).aupdate(
            status="completed", progress=100, completed_at=timezone.now()
        )

    @staticmethod
    async def _reset_connections() -> None:
        """Drop database connections that died (a failover, a restart): this
        process runs for days, and Django only does it between requests."""
        from asgiref.sync import sync_to_async
        from django.db import close_old_connections

        with contextlib.suppress(Exception):
            await sync_to_async(close_old_connections)()

    async def _failed(self, update: dict, error: Exception) -> None:
        """Tell the chat its message was not handled, so it is not lost silently."""
        message = update.get("message") or (update.get("callback_query") or {}).get(
            "message"
        )
        chat_id = ((message or {}).get("chat") or {}).get("id")
        if chat_id is None:
            return
        with contextlib.suppress(Exception):
            await self.send_text(
                chat_id,
                f"Sorry, I couldn't handle that message ({type(error).__name__}). "
                "Please send it again.",
            )

    async def replay(self) -> int:
        """Handle saved updates that were never started (the process died or
        the database failed first), and forget old handled ones. Returns how
        many it ran."""
        from django.utils import timezone

        from django_ergo.conversation.models import BotJob

        now = timezone.now()
        await BotJob.objects.filter(
            bot_name=self.bot.name,
            target=JOB_TARGET,
            status="completed",
            created_at__lt=now - KEEP_HANDLED,
        ).adelete()
        cutoff = now - REPLAY_AFTER
        jobs = [
            job
            async for job in BotJob.objects.filter(
                bot_name=self.bot.name,
                target=JOB_TARGET,
                status="pending",
                created_at__lt=cutoff,
            ).order_by("created_at")
        ]
        for job in jobs:
            logger.info("Replaying Telegram %s", job.name)
            await self.process(job)
        return len(jobs)

    def _replay_soon(self) -> None:
        """Replay in the background, at most once a minute per process."""
        now = time.monotonic()
        if self._replaying or now - self._replayed_at < REPLAY_EVERY:
            return
        self._replaying, self._replayed_at = True, now

        async def run():
            try:
                await self.replay()
            except Exception:
                logger.exception("Replaying Telegram updates failed")
            finally:
                self._replaying = False

        self._spawn(run())

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def poll_once(self) -> int:
        updates = await self.api.call(
            "getUpdates",
            offset=self._offset,
            timeout=self.poll_timeout,
            allowed_updates=["message", "callback_query"],
        )
        for update in updates:
            # Saved before the offset moves on: if saving fails, Telegram
            # sends the update again on the next poll.
            job = await self.accept(update)
            self._offset = max(self._offset, update["update_id"] + 1)
            if job is not None:
                await self.process(job)
        self._replay_soon()
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
        if not isinstance(update, dict) or "update_id" not in update:
            return HttpResponse(status=400)
        try:
            job = await self.accept(update)
        except Exception:
            # Not saved: a 500 makes Telegram send it again later.
            logger.exception("Could not save Telegram update %s", update["update_id"])
            return HttpResponse(status=500)
        # Answer Telegram at once; the turn runs after the response.
        if job is not None:
            self._spawn(self.process(job))
        self._replay_soon()
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
        try:
            await self.replay()
        except Exception:
            logger.exception("Replaying Telegram updates failed")
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
                await self._reset_connections()
                await asyncio.sleep(5)


def _is_uuid(value: str) -> bool:
    import uuid

    try:
        uuid.UUID(value)
    except ValueError:
        return False
    return True


def approvals_stamp(tool_use_ids: list[str]) -> str:
    """A short fingerprint of what a call waits on (Telegram button data is 64 bytes)."""
    return hashlib.sha256(",".join(sorted(tool_use_ids)).encode()).hexdigest()[:8]
