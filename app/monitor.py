from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from telethon import TelegramClient, events
from telethon.errors import FloodWaitError
from telethon.tl.types import Channel, Chat, Message, MessageActionChatMigrateTo, MessageService

from app.config import Settings
from app.database import Database, MonitoredChat

logger = logging.getLogger(__name__)
UTC = timezone.utc


def chat_url(entity: object, chat_id: int, message_id: int | None) -> str | None:
    if message_id is None:
        return None
    username = getattr(entity, "username", None)
    if username:
        return f"https://t.me/{username}/{message_id}"
    if isinstance(entity, Channel):
        # Telegram's private-group deep link; it opens for group members only.
        internal_id = str(chat_id).removeprefix("-100")
        return f"https://t.me/c/{internal_id}/{message_id}"
    # Legacy basic groups have no web link. Telegram Desktop/mobile understands this URI.
    return f"tg://openmessage?chat_id={chat_id}&message_id={message_id}"


class AccountLogin:
    """Keeps the user-account connection and a short-lived login challenge."""

    def __init__(self, settings: Settings) -> None:
        settings.session_path.parent.mkdir(parents=True, exist_ok=True)
        self.settings = settings
        self.client = TelegramClient(str(settings.session_path), settings.api_id, settings.api_hash)
        self.waiting_for_code = False
        self.waiting_for_password = False
        self._lock = asyncio.Lock()

    async def is_authorized(self) -> bool:
        await self.client.connect()
        return await self.client.is_user_authorized()

    async def request_code(self) -> None:
        async with self._lock:
            await self.client.connect()
            if await self.client.is_user_authorized():
                return
            await self.client.send_code_request(self.settings.phone)
            self.waiting_for_code = True
            self.waiting_for_password = False

    async def submit_code(self, code: str) -> str:
        from telethon.errors import SessionPasswordNeededError

        async with self._lock:
            if not self.waiting_for_code:
                return "no_pending_code"
            try:
                await self.client.sign_in(self.settings.phone, code)
            except SessionPasswordNeededError:
                self.waiting_for_code = False
                self.waiting_for_password = True
                return "password_needed"
            self.waiting_for_code = False
            return "authorized"

    async def submit_password(self, password: str) -> bool:
        async with self._lock:
            if not self.waiting_for_password:
                return False
            await self.client.sign_in(password=password)
            self.waiting_for_password = False
            return True

    async def disconnect(self) -> None:
        await self.client.disconnect()


class InactivityMonitor:
    def __init__(self, settings: Settings, database: Database, login: AccountLogin, bot: Bot) -> None:
        self.settings = settings
        self.database = database
        self.login = login
        self.bot = bot
        self._scan_lock = asyncio.Lock()
        self._listener_started = False
        self._last_full_sync: datetime | None = None

    async def run_cycle(self, force_full_sync: bool = False) -> tuple[int, int]:
        """Use local state for alerts; request Telegram's dialog list only once daily."""
        target_chat_id = self.database.target_chat_id()
        if target_chat_id is None:
            logger.info("Skipping scan: CRM group has not been configured")
            return 0, 0
        if not await self.login.is_authorized():
            logger.info("Skipping scan: monitored account is not authorized")
            return 0, 0

        async with self._scan_lock:
            await self._start_update_listener()
            observed = 0
            now = datetime.now(UTC)
            should_full_sync = force_full_sync or self._last_full_sync is None
            if self._last_full_sync is not None:
                should_full_sync = should_full_sync or now - self._last_full_sync >= timedelta(
                    hours=self.settings.full_sync_interval_hours
                )
            if should_full_sync:
                observed = await self._sync_catalog(target_chat_id)
                self._last_full_sync = now
            cutoff = datetime.now(UTC) - timedelta(days=self.settings.inactivity_days)
            alerts = 0
            priority_chat_id = self.database.get_setting("priority_chat_id")
            priority = int(priority_chat_id) if priority_chat_id else None
            stale_chats = self.database.stale_unnotified_chats(
                cutoff, priority
            )[: self.settings.max_alerts_per_scan]
            for index, chat in enumerate(stale_chats):
                await self._send_alert_with_backoff(target_chat_id, chat)
                if chat.chat_id == priority:
                    self.database.set_setting("priority_chat_id", "")
                alerts += 1
                if index < len(stale_chats) - 1:
                    await asyncio.sleep(self.settings.alert_send_delay_seconds)
            logger.info("Cycle complete: %s groups synchronized, %s new alerts", observed, alerts)
            return observed, alerts

    async def _sync_catalog(self, target_chat_id: int) -> int:
        """The only periodic request to the monitored account: one dialog-list sync."""
        observed = 0
        dialogs = await self.login.client.get_dialogs()
        for dialog in dialogs:
            if not dialog.is_group or dialog.id == target_chat_id:
                continue
            if isinstance(dialog.entity, Chat) and dialog.entity.migrated_to:
                await self._retire_migrated_chat(target_chat_id, dialog.id)
                continue
            existing = self.database.get_chat(dialog.id)
            if existing and existing.is_excluded:
                continue
            last_message = await self._last_client_message(dialog.entity, dialog.message)
            cutoff = datetime.now(UTC) - timedelta(days=self.settings.inactivity_days)
            # A group with no messages is still tracked: its three-day timer starts
            # when it is first discovered, rather than alerting immediately.
            activity_at = last_message.date if last_message else (existing.last_activity_at if existing else datetime.now(UTC))
            if existing and not existing.client_activity_initialized and last_message is None:
                # The old timestamp may belong to a staff member or a bot. If no
                # client wrote in the last three days, this chat is already stale.
                activity_at = min(activity_at or cutoff, cutoff)
            message_id = last_message.id if last_message else (existing.last_message_id if existing else None)
            if (
                existing and last_message and existing.last_activity_at
                and last_message.date > existing.last_activity_at
            ):
                await self._delete_open_card_after_client_message(target_chat_id, existing)
            chat = self.database.upsert_chat(
                chat_id=dialog.id,
                title=dialog.name or "Без названия",
                url=chat_url(dialog.entity, dialog.id, message_id),
                # A service or staff message must not replace the last client date.
                last_activity_at=activity_at,
                last_message_id=message_id,
            )
            if existing and (
                existing.title != chat.title or existing.url != chat.url
                or (existing.notification_message_id and existing.last_activity_at != chat.last_activity_at)
            ):
                await self._refresh_existing_card(target_chat_id, chat)
            observed += 1
        return observed

    async def _start_update_listener(self) -> None:
        if self._listener_started:
            return
        self.login.client.add_event_handler(self._on_new_message, events.NewMessage())
        self._listener_started = True
        logger.info("Live Telegram activity listener started")

    async def _is_client_message(self, message: Message) -> bool:
        """Identify clients by stable Telegram ID, never by a mutable username."""
        if message.out or message.sender_id is None or message.sender_id <= 0:
            return False
        if message.sender_id in self.settings.team_user_ids:
            return False
        sender = await message.get_sender()
        return sender is not None and not getattr(sender, "bot", False)

    async def _last_client_message(self, entity: object, latest: object) -> Message | None:
        """Look back only through the inactivity window when staff or bots wrote last."""
        cutoff = datetime.now(UTC) - timedelta(days=self.settings.inactivity_days)
        if isinstance(latest, Message) and await self._is_client_message(latest):
            return latest
        if latest is None or getattr(latest, "date", cutoff) < cutoff:
            return None
        async for candidate in self.login.client.iter_messages(entity):
            if isinstance(candidate, MessageService):
                continue
            if not isinstance(candidate, Message):
                continue
            if await self._is_client_message(candidate):
                return candidate
            if candidate.date < cutoff:
                break
        return None

    async def _delete_open_card_after_client_message(self, target_chat_id: int, chat: MonitoredChat) -> None:
        """Remove an open CRM task as soon as a client has participated."""
        if chat.notification_message_id is None:
            return
        try:
            await self.bot.delete_message(target_chat_id, chat.notification_message_id)
        except TelegramBadRequest as error:
            logger.warning(
                "Could not delete CRM card %s after client replied in chat %s: %s",
                chat.notification_message_id,
                chat.chat_id,
                error.message,
            )
        else:
            logger.info(
                "Deleted CRM card %s after client replied in chat %s",
                chat.notification_message_id,
                chat.chat_id,
            )

    async def _retire_migrated_chat(self, target_chat_id: int, chat_id: int) -> None:
        """Drop the obsolete pre-migration group so it never creates a duplicate task."""
        chat = self.database.get_chat(chat_id)
        if chat is None or chat.is_excluded:
            return
        if chat.notification_message_id is not None:
            try:
                await self.bot.delete_message(target_chat_id, chat.notification_message_id)
            except TelegramBadRequest as error:
                logger.warning(
                    "Could not delete legacy migration card %s for chat %s: %s",
                    chat.notification_message_id,
                    chat_id,
                    error.message,
                )
        self.database.retire_migrated_chat(chat_id)
        logger.info("Stopped monitoring legacy chat %s after Telegram migration", chat_id)

    async def _on_new_message(self, event: events.NewMessage.Event) -> None:
        """Telegram pushes this update; it is not a periodic polling request."""
        target_chat_id = self.database.target_chat_id()
        if target_chat_id is None or not event.is_group or event.chat_id == target_chat_id:
            return
        message = event.message
        if isinstance(message, MessageService):
            # A service update (for example, the account being added to a new group)
            # starts monitoring an otherwise empty new group without treating it as chat activity.
            known = self.database.get_chat(event.chat_id)
            if isinstance(message.action, MessageActionChatMigrateTo):
                await self._retire_migrated_chat(target_chat_id, event.chat_id)
                return
            if known is None:
                entity = await event.get_chat()
                self.database.upsert_chat(
                    chat_id=event.chat_id,
                    title=getattr(entity, "title", "Без названия"),
                    url=chat_url(entity, event.chat_id, message.id),
                    last_activity_at=message.date,
                    last_message_id=message.id,
                )
            else:
                # Renaming a group is a Telegram service event, not a client message.
                # Preserve the inactivity timer and only refresh its displayed title.
                entity = await event.get_chat()
                title = getattr(entity, "title", known.title) or known.title
                if title != known.title:
                    chat = self.database.update_chat_title(event.chat_id, title)
                    if chat is not None:
                        await self._refresh_existing_card(target_chat_id, chat)
            return
        if not isinstance(message, Message):
            return
        known = self.database.get_chat(event.chat_id)
        if not await self._is_client_message(message):
            logger.info("Ignored non-client message in chat %s", event.chat_id)
            return
        if known is not None:
            await self._delete_open_card_after_client_message(target_chat_id, known)
            self.database.record_activity(event.chat_id, message.date, message.id)
            return
        # A group added after the initial inventory is discovered from its first update.
        entity = await event.get_chat()
        self.database.upsert_chat(
            chat_id=event.chat_id,
            title=getattr(entity, "title", "Без названия"),
            url=chat_url(entity, event.chat_id, message.id),
            last_activity_at=message.date,
            last_message_id=message.id,
        )

    async def _send_alert(self, target_chat_id: int, chat: MonitoredChat) -> None:
        """Send one card per inactivity cycle, preserving any existing card."""
        assert chat.last_activity_at is not None
        if chat.notification_message_id is not None:
            # Repair an inconsistent row without creating a duplicate message.
            self.database.mark_notified(
                chat.chat_id, chat.last_activity_at,
                chat.notification_message_id, datetime.now(UTC),
            )
            return
        inactivity = _format_duration(datetime.now(UTC) - chat.last_activity_at)
        text = (
            f"<b>{_escape(chat.title)}</b> — нет активности <b>{inactivity}</b>."
        )
        message = await self.bot.send_message(
            target_chat_id,
            text,
            reply_markup=alert_keyboard(chat),
        )
        self.database.mark_notified(chat.chat_id, chat.last_activity_at, message.message_id, datetime.now(UTC))

    async def _refresh_existing_card(self, target_chat_id: int, chat: MonitoredChat) -> None:
        """Keep an open CRM card accurate when Telegram changes group metadata."""
        if chat.notification_message_id is None or chat.last_activity_at is None:
            return
        inactivity = _format_duration(datetime.now(UTC) - chat.last_activity_at)
        text = f"<b>{_escape(chat.title)}</b> — нет активности <b>{inactivity}</b>."
        try:
            await self.bot.edit_message_text(
                chat_id=target_chat_id,
                message_id=chat.notification_message_id,
                text=text,
                reply_markup=alert_keyboard(chat),
            )
        except TelegramBadRequest as error:
            # The card may have been manually deleted. Keep the sent marker so it
            # cannot be recreated as an unwanted repeat.
            logger.warning(
                "Could not refresh card %s after metadata update for chat %s: %s",
                chat.notification_message_id,
                chat.chat_id,
                error.message,
            )
        else:
            logger.info("Refreshed CRM card %s after metadata update for chat %s", chat.notification_message_id, chat.chat_id)

    async def _send_alert_with_backoff(self, target_chat_id: int, chat: MonitoredChat) -> None:
        while True:
            try:
                await self._send_alert(target_chat_id, chat)
                return
            except TelegramRetryAfter as error:
                logger.warning("Bot rate limit: pausing for %s seconds", error.retry_after)
                await asyncio.sleep(error.retry_after + 1)

    async def rebuild_alert_cards(self) -> int:
        """Delete prior bot alert cards and put their chats back in the alert queue."""
        target_chat_id = self.database.target_chat_id()
        if target_chat_id is None:
            return 0
        removed = 0
        for chat_id, message_id in self.database.notification_cards():
            try:
                await self.bot.delete_message(target_chat_id, message_id)
            except TelegramBadRequest:
                logger.warning("Could not delete old alert card %s", message_id)
                continue
            self.database.clear_notification(chat_id)
            removed += 1
            await asyncio.sleep(1)
        logger.info("Deleted %s previous alert cards", removed)
        return removed

    async def replace_legacy_card_buttons(self) -> int:
        """Replace unsupported basic-group links on existing CRM cards."""
        target_chat_id = self.database.target_chat_id()
        if target_chat_id is None:
            return 0
        updated = 0
        for chat_id, message_id in self.database.notification_cards():
            chat = self.database.get_chat(chat_id)
            if chat is None or not _is_legacy_group_link(chat.url):
                continue
            try:
                await self.bot.edit_message_reply_markup(
                    chat_id=target_chat_id,
                    message_id=message_id,
                    reply_markup=alert_keyboard(chat),
                )
            except TelegramBadRequest:
                logger.warning("Could not update buttons on alert card %s", message_id)
                continue
            updated += 1
            # Edits use the same group rate-limit budget as messages.
            await asyncio.sleep(4)
        logger.info("Replaced unsupported links on %s legacy-group cards", updated)
        return updated

    async def run_forever(self) -> None:
        while True:
            try:
                await self.run_cycle()
            except FloodWaitError as error:
                # Telegram explicitly tells us how long to back off. Never retry early.
                logger.warning("Telegram rate limit: pausing for %s seconds", error.seconds)
                await asyncio.sleep(error.seconds + 5)
                continue
            except Exception:
                logger.exception("Inactivity scan failed")
            await asyncio.sleep(self.settings.check_interval_minutes * 60)


def _escape(value: str) -> str:
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _format_duration(duration: timedelta) -> str:
    total_minutes = max(0, int(duration.total_seconds() // 60))
    days, remaining_minutes = divmod(total_minutes, 24 * 60)
    hours, minutes = divmod(remaining_minutes, 60)
    parts: list[str] = []
    if days:
        parts.append(f"{days} д.")
    if hours or days:
        parts.append(f"{hours} ч.")
    parts.append(f"{minutes} мин.")
    return " ".join(parts)


def alert_keyboard(chat: MonitoredChat) -> InlineKeyboardMarkup:
    if chat.url:
        first_button = InlineKeyboardButton(text="Открыть чат", url=chat.url)
    else:
        first_button = InlineKeyboardButton(text="Открыть чат", callback_data=f"find:{chat.chat_id}")
    return InlineKeyboardMarkup(
        inline_keyboard=[[
            first_button,
            InlineKeyboardButton(text="✅ Готово", callback_data=f"done:{chat.chat_id}"),
            InlineKeyboardButton(text="Не отслеживать", callback_data=f"exclude:{chat.chat_id}"),
        ]]
    )


def _is_legacy_group_link(url: str | None) -> bool:
    return bool(url and url.startswith("tg://openmessage"))
