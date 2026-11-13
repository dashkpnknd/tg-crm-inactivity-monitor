from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


def _required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Environment variable {name} is required")
    return value


@dataclass(frozen=True)
class Settings:
    api_id: int
    api_hash: str
    phone: str
    bot_token: str
    controller_user_ids: frozenset[int]
    inactivity_days: int
    check_interval_minutes: int
    full_sync_interval_hours: int
    max_alerts_per_scan: int
    alert_send_delay_seconds: float
    database_path: Path
    session_path: Path

    @classmethod
    def from_env(cls) -> "Settings":
        load_dotenv()
        controller_ids = frozenset(
            int(value.strip())
            for value in os.getenv("CONTROLLER_USER_IDS", "").split(",")
            if value.strip()
        )
        inactivity_days = int(os.getenv("INACTIVITY_DAYS", "3"))
        check_interval_minutes = int(os.getenv("CHECK_INTERVAL_MINUTES", "1"))
        full_sync_interval_hours = int(os.getenv("FULL_SYNC_INTERVAL_HOURS", "24"))
        max_alerts_per_scan = int(os.getenv("MAX_ALERTS_PER_SCAN", "1"))
        alert_send_delay_seconds = float(os.getenv("ALERT_SEND_DELAY_SECONDS", "60"))
        if inactivity_days < 1 or check_interval_minutes < 1:
            raise RuntimeError("INACTIVITY_DAYS and CHECK_INTERVAL_MINUTES must be positive")
        if full_sync_interval_hours < 1:
            raise RuntimeError("FULL_SYNC_INTERVAL_HOURS must be positive")
        if max_alerts_per_scan != 1 or alert_send_delay_seconds < 60:
            raise RuntimeError("Set MAX_ALERTS_PER_SCAN=1 and ALERT_SEND_DELAY_SECONDS to at least 60")
        return cls(
            api_id=int(_required("TELEGRAM_API_ID")),
            api_hash=_required("TELEGRAM_API_HASH"),
            phone=_required("KSUSHA_PHONE"),
            bot_token=_required("BOT_TOKEN"),
            controller_user_ids=controller_ids,
            inactivity_days=inactivity_days,
            check_interval_minutes=check_interval_minutes,
            full_sync_interval_hours=full_sync_interval_hours,
            max_alerts_per_scan=max_alerts_per_scan,
            alert_send_delay_seconds=alert_send_delay_seconds,
            database_path=Path(os.getenv("DATABASE_PATH", "data/crm_monitor.sqlite3")),
            session_path=Path(os.getenv("SESSION_PATH", "data/ksusha")),
        )
