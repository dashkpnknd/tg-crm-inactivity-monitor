import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import DeleteMessage
from telethon.tl.types import Message, PeerChat, PeerUser, User

from app.database import Database
from app.monitor import InactivityMonitor


UTC = timezone.utc


def message(message_id, sender_id, date, *, bot=False, out=False, username=None):
    value = Message(
        id=message_id,
        peer_id=PeerChat(100),
        from_id=PeerUser(sender_id),
        date=date,
        message="test",
        out=out,
    )
    value._sender = User(id=sender_id, bot=bot, username=username)
    return value


class ClientActivityTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.database = Database(Path(self.tempdir.name) / "monitor.sqlite3")
        self.database.initialize()
        self.database.set_target_chat_id(999)
        self.addCleanup(self.database.close)
        self.settings = SimpleNamespace(inactivity_days=3)
        self.bot = SimpleNamespace(
            delete_message=AsyncMock(), edit_message_text=AsyncMock(),
            send_message=AsyncMock(),
        )
        self.client = SimpleNamespace()
        self.monitor = InactivityMonitor(
            self.settings, self.database, SimpleNamespace(client=self.client), self.bot
        )

    async def test_staff_and_other_bots_count_but_avito_report_does_not(self):
        now = datetime.now(UTC)
        for username in ("first_name", "second_name"):
            with self.subTest(username=username):
                staff = message(1, 123, now, username=username)
                self.assertTrue(await self.monitor._is_relevant_message(staff))
        self.assertTrue(await self.monitor._is_relevant_message(message(2, 456, now, bot=True)))
        self.assertTrue(await self.monitor._is_relevant_message(message(3, 456, now, out=True)))
        self.assertFalse(await self.monitor._is_relevant_message(
            message(4, 789, now, bot=True, username="AvitoReportStat_bot")
        ))
        self.assertFalse(await self.monitor._is_relevant_message(
            message(6, 8721424338, now, bot=True)
        ))
        self.assertTrue(await self.monitor._is_relevant_message(message(5, 456, now)))

    async def test_catalog_looks_past_avito_report_to_team_message(self):
        now = datetime.now(UTC)
        client = message(1, 456, now - timedelta(days=2))
        staff = message(2, 123, now - timedelta(days=1))
        avito = message(3, 789, now, bot=True, username="avitoReportStat_bot")

        async def iter_messages(_entity):
            for item in (avito, staff, client):
                yield item

        self.client.iter_messages = iter_messages
        self.assertIs(await self.monitor._last_relevant_message(object(), avito), staff)

    async def test_existing_avito_only_chat_becomes_stale_without_losing_card(self):
        now = datetime.now(UTC)
        avito = message(3, 789, now, bot=True, username="avitoReportStat_bot")
        self.database.connection.execute(
            "INSERT INTO chats(chat_id,title,last_activity_at,last_message_id,"
            "notification_message_id,last_alert_sent_at) VALUES (100,'Client',?,?,?,?)",
            (avito.date.isoformat(), avito.id, 90, now.isoformat()),
        )
        self.database.connection.commit()

        async def iter_messages(_entity):
            yield avito

        self.client.iter_messages = iter_messages
        self.client.get_dialogs = AsyncMock(return_value=[SimpleNamespace(
            is_group=True, id=100, entity=SimpleNamespace(title="Client"),
            message=avito, name="Client",
        )])
        await self.monitor._sync_catalog(999)

        chat = self.database.get_chat(100)
        self.assertTrue(chat.client_activity_initialized)
        self.assertLessEqual(chat.last_activity_at, datetime.now(UTC) - timedelta(days=3))
        self.assertEqual(chat.notification_message_id, 90)
        self.bot.delete_message.assert_not_awaited()

    async def test_client_message_clears_open_card_and_updates_timer(self):
        now = datetime.now(UTC)
        self.database.upsert_chat(100, "Client", None, now - timedelta(days=4), 1)
        self.database.mark_notified(100, now - timedelta(days=4), 90, now)
        client = message(2, 456, now)
        event = SimpleNamespace(is_group=True, chat_id=100, message=client)

        await self.monitor._on_new_message(event)

        self.bot.delete_message.assert_awaited_once_with(999, 90)
        chat = self.database.get_chat(100)
        self.assertEqual(chat.last_message_id, 2)
        self.assertIsNone(chat.notification_message_id)

    async def test_staff_message_updates_timer_and_closes_open_card(self):
        now = datetime.now(UTC)
        old_activity = now - timedelta(days=4)
        self.database.upsert_chat(100, "Client", None, old_activity, 1)
        self.database.mark_notified(100, old_activity, 90, now)
        staff = message(2, 123, now, username="second_name")

        await self.monitor._on_new_message(
            SimpleNamespace(is_group=True, chat_id=100, message=staff)
        )

        chat = self.database.get_chat(100)
        self.assertEqual(chat.last_activity_at, now)
        self.assertIsNone(chat.notification_message_id)
        self.bot.delete_message.assert_awaited_once_with(999, 90)

    async def test_old_card_uses_admin_fallback_after_bot_api_refuses(self):
        self.bot.delete_message.side_effect = TelegramBadRequest(
            method=DeleteMessage(chat_id=999, message_id=90),
            message="message can't be deleted",
        )
        self.bot.me = AsyncMock(return_value=SimpleNamespace(id=8804717222))
        self.client.get_messages = AsyncMock(return_value=SimpleNamespace(sender_id=8804717222))
        self.client.delete_messages = AsyncMock()

        self.assertTrue(await self.monitor.delete_crm_card(999, 90))

        self.client.get_messages.assert_awaited_once_with(999, ids=90)
        self.client.delete_messages.assert_awaited_once_with(999, [90], revoke=True)

    async def test_admin_fallback_refuses_messages_from_other_senders(self):
        self.bot.delete_message.side_effect = TelegramBadRequest(
            method=DeleteMessage(chat_id=999, message_id=90),
            message="message can't be deleted",
        )
        self.bot.me = AsyncMock(return_value=SimpleNamespace(id=8804717222))
        self.client.get_messages = AsyncMock(return_value=SimpleNamespace(sender_id=123))
        self.client.delete_messages = AsyncMock()

        self.assertFalse(await self.monitor.delete_crm_card(999, 90))

        self.client.delete_messages.assert_not_awaited()

    async def test_alert_is_sent_once_until_client_starts_new_cycle(self):
        now = datetime.now(UTC)
        old_activity = now - timedelta(days=4)
        self.database.upsert_chat(100, "Client", None, old_activity, 1)
        self.assertEqual(
            len(self.database.stale_unnotified_chats(now - timedelta(days=3))), 1
        )

        self.database.mark_notified(100, old_activity, 90, now)
        self.assertEqual(
            self.database.stale_unnotified_chats(now + timedelta(days=30)), []
        )
        await self.monitor._send_alert(999, self.database.get_chat(100))
        self.bot.send_message.assert_not_awaited()
        self.bot.delete_message.assert_not_awaited()

        self.database.record_activity(100, now, 2)
        self.assertEqual(
            len(self.database.stale_unnotified_chats(now - timedelta(days=2))), 0
        )
        self.assertEqual(
            len(self.database.stale_unnotified_chats(now + timedelta(days=1))), 1
        )


if __name__ == "__main__":
    unittest.main()
