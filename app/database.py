from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


@dataclass(frozen=True)
class MonitoredChat:
    chat_id: int
    title: str
    url: str | None
    last_activity_at: datetime | None
    last_message_id: int | None
    is_excluded: bool
    notified_for_activity_at: datetime | None
    notification_message_id: int | None


class Database:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path)
        self.connection.row_factory = sqlite3.Row

    def initialize(self) -> None:
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS chats (
                chat_id INTEGER PRIMARY KEY,
                title TEXT NOT NULL,
                url TEXT,
                last_activity_at TEXT,
                last_message_id INTEGER,
                is_excluded INTEGER NOT NULL DEFAULT 0,
                notified_for_activity_at TEXT,
                notification_message_id INTEGER
                ,last_alert_sent_at TEXT
                ,resolved_for_activity_at TEXT
            );
            """
        )
        try:
            self.connection.execute("ALTER TABLE chats ADD COLUMN last_message_id INTEGER")
        except sqlite3.OperationalError:
            pass
        for column in ("last_alert_sent_at", "resolved_for_activity_at"):
            try:
                self.connection.execute(f"ALTER TABLE chats ADD COLUMN {column} TEXT")
            except sqlite3.OperationalError:
                pass
        # Cards sent before reminders were introduced should not all repeat on the next restart.
        self.connection.execute(
            "UPDATE chats SET last_alert_sent_at = ? "
            "WHERE last_alert_sent_at IS NULL AND notified_for_activity_at IS NOT NULL",
            (datetime.now(timezone.utc).isoformat(),),
        )
        self.connection.commit()

    def close(self) -> None:
        self.connection.close()

    def get_setting(self, key: str) -> str | None:
        row = self.connection.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def set_setting(self, key: str, value: str | int) -> None:
        self.connection.execute(
            "INSERT INTO settings(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, str(value)),
        )
        self.connection.commit()

    def is_controller(self, user_id: int, configured_ids: frozenset[int]) -> bool:
        saved = self.get_setting("controller_user_id")
        return user_id in configured_ids or (saved is not None and int(saved) == user_id)

    def set_controller_if_empty(self, user_id: int) -> bool:
        if self.get_setting("controller_user_id") is not None:
            return False
        self.set_setting("controller_user_id", user_id)
        return True

    def target_chat_id(self) -> int | None:
        value = self.get_setting("target_chat_id")
        return int(value) if value else None

    def set_target_chat_id(self, chat_id: int) -> None:
        self.set_setting("target_chat_id", chat_id)

    def upsert_chat(
        self,
        chat_id: int,
        title: str,
        url: str | None,
        last_activity_at: datetime | None,
        last_message_id: int | None,
    ) -> MonitoredChat:
        existing = self.get_chat(chat_id)
        last_activity_text = last_activity_at.isoformat() if last_activity_at else None
        if existing and existing.last_activity_at != last_activity_at:
            # A new actual message starts a new inactivity cycle.
            self.connection.execute(
                "UPDATE chats SET title=?, url=?, last_activity_at=?, "
                "last_message_id=?, notified_for_activity_at=NULL, notification_message_id=NULL, "
                "last_alert_sent_at=NULL, resolved_for_activity_at=NULL WHERE chat_id=?",
                (title, url, last_activity_text, last_message_id, chat_id),
            )
        else:
            self.connection.execute(
                "INSERT INTO chats(chat_id, title, url, last_activity_at, last_message_id) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(chat_id) DO UPDATE SET title=excluded.title, url=excluded.url, "
                "last_message_id=excluded.last_message_id",
                (chat_id, title, url, last_activity_text, last_message_id),
            )
        self.connection.commit()
        return self.get_chat(chat_id)  # type: ignore[return-value]

    def get_chat(self, chat_id: int) -> MonitoredChat | None:
        row = self.connection.execute("SELECT * FROM chats WHERE chat_id = ?", (chat_id,)).fetchone()
        return self._chat_from_row(row) if row else None

    def update_chat_title(self, chat_id: int, title: str) -> MonitoredChat | None:
        """Store a rename without treating its service message as client activity."""
        self.connection.execute("UPDATE chats SET title = ? WHERE chat_id = ?", (title, chat_id))
        self.connection.commit()
        return self.get_chat(chat_id)

    def record_activity(self, chat_id: int, activity_at: datetime, message_id: int) -> None:
        """Start a fresh inactivity cycle after a live incoming message."""
        existing = self.get_chat(chat_id)
        if existing is None or (existing.last_activity_at == activity_at and existing.last_message_id == message_id):
            return
        self.connection.execute(
            "UPDATE chats SET last_activity_at=?, last_message_id=?, notified_for_activity_at=NULL, "
            "notification_message_id=NULL, last_alert_sent_at=NULL, resolved_for_activity_at=NULL WHERE chat_id=?",
            (activity_at.isoformat(), message_id, chat_id),
        )
        self.connection.commit()

    def stale_unnotified_chats(
        self, cutoff: datetime, reminder_cutoff: datetime, priority_chat_id: int | None = None
    ) -> list[MonitoredChat]:
        rows = self.connection.execute(
            "SELECT * FROM chats WHERE is_excluded = 0 AND last_activity_at IS NOT NULL "
            "AND last_activity_at <= ? AND (resolved_for_activity_at IS NULL "
            "OR resolved_for_activity_at <> last_activity_at) "
            "AND (last_alert_sent_at IS NULL OR last_alert_sent_at <= ?) "
            "ORDER BY CASE WHEN chat_id = ? THEN 0 ELSE 1 END, last_activity_at DESC",
            (cutoff.isoformat(), reminder_cutoff.isoformat(), priority_chat_id),
        ).fetchall()
        return [self._chat_from_row(row) for row in rows]

    def mark_notified(self, chat_id: int, activity_at: datetime, message_id: int, sent_at: datetime) -> None:
        self.connection.execute(
            "UPDATE chats SET notified_for_activity_at=?, notification_message_id=?, last_alert_sent_at=? WHERE chat_id=?",
            (activity_at.isoformat(), message_id, sent_at.isoformat(), chat_id),
        )
        self.connection.commit()

    def notification_cards(self) -> list[tuple[int, int]]:
        rows = self.connection.execute(
            "SELECT chat_id, notification_message_id FROM chats WHERE notification_message_id IS NOT NULL"
        ).fetchall()
        return [(row["chat_id"], row["notification_message_id"]) for row in rows]

    def clear_notification(self, chat_id: int) -> None:
        self.connection.execute(
            "UPDATE chats SET notified_for_activity_at=NULL, notification_message_id=NULL, "
            "last_alert_sent_at=NULL, resolved_for_activity_at=NULL WHERE chat_id=?",
            (chat_id,),
        )
        self.connection.commit()

    def resolve(self, chat_id: int) -> MonitoredChat | None:
        chat = self.get_chat(chat_id)
        if chat is None or chat.last_activity_at is None:
            return chat
        self.connection.execute(
            "UPDATE chats SET resolved_for_activity_at=?, notification_message_id=NULL WHERE chat_id=?",
            (chat.last_activity_at.isoformat(), chat_id),
        )
        self.connection.commit()
        return self.get_chat(chat_id)

    def exclude(self, chat_id: int) -> MonitoredChat | None:
        self.connection.execute("UPDATE chats SET is_excluded = 1 WHERE chat_id = ?", (chat_id,))
        self.connection.commit()
        return self.get_chat(chat_id)

    def retire_migrated_chat(self, chat_id: int) -> None:
        """Stop monitoring a legacy group that Telegram has migrated to a supergroup."""
        self.connection.execute(
            "UPDATE chats SET is_excluded=1, notified_for_activity_at=NULL, "
            "notification_message_id=NULL, last_alert_sent_at=NULL, resolved_for_activity_at=NULL "
            "WHERE chat_id=?",
            (chat_id,),
        )
        self.connection.commit()

    @staticmethod
    def _chat_from_row(row: sqlite3.Row) -> MonitoredChat:
        def date(value: str | None) -> datetime | None:
            return datetime.fromisoformat(value) if value else None

        return MonitoredChat(
            chat_id=row["chat_id"],
            title=row["title"],
            url=row["url"],
            last_activity_at=date(row["last_activity_at"]),
            last_message_id=row["last_message_id"],
            is_excluded=bool(row["is_excluded"]),
            notified_for_activity_at=date(row["notified_for_activity_at"]),
            notification_message_id=row["notification_message_id"],
        )
