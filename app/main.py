from __future__ import annotations

import asyncio
import logging
import os
import re

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, Message
from telethon.errors import PasswordHashInvalidError, PhoneCodeExpiredError, PhoneCodeInvalidError

from app.config import Settings
from app.database import Database
from app.monitor import AccountLogin, InactivityMonitor

logger = logging.getLogger(__name__)


def private_controller(message: Message, database: Database, settings: Settings) -> bool:
    return (
        message.chat.type == "private"
        and message.from_user is not None
        and database.is_controller(message.from_user.id, settings.controller_user_ids)
    )


async def remove_sensitive_message(bot: Bot, message: Message) -> None:
    try:
        await bot.delete_message(message.chat.id, message.message_id)
    except Exception:
        logger.warning("Could not delete sensitive login message", exc_info=True)


def create_router(
    bot: Bot, database: Database, settings: Settings, login: AccountLogin, monitor: InactivityMonitor
) -> Router:
    router = Router()

    @router.message(Command("setup"))
    async def setup(message: Message) -> None:
        if message.chat.type == "private" or message.from_user is None:
            await message.answer("Команду /setup нужно отправить в группу CRM.")
            return
        allowed = database.is_controller(message.from_user.id, settings.controller_user_ids)
        if not allowed and not database.set_controller_if_empty(message.from_user.id):
            await message.answer("Настраивать CRM может только назначенный ответственный.")
            return
        database.set_target_chat_id(message.chat.id)
        await message.answer(
            "Группа CRM подключена. Теперь откройте личный диалог с ботом и отправьте /start — "
            "я запрошу код для входа в аккаунт Ксюши."
        )

    @router.message(CommandStart())
    async def start(message: Message) -> None:
        if not private_controller(message, database, settings):
            await message.answer("Доступ к настройке выдаётся ответственному из группы CRM.")
            return
        if await login.is_authorized():
            await message.answer("Аккаунт Ксюши уже подключён. Мониторинг активен.")
            return
        await message.answer("Сейчас запрошу одноразовый код в Telegram-аккаунте Ксюши.")
        try:
            await login.request_code()
        except Exception:
            logger.exception("Could not request Telegram login code")
            await message.answer("Не удалось запросить код. Проверьте настройки и попробуйте позже.")
            return
        await message.answer(
            "Код отправлен в Telegram Ксюши. Пришлите сюда только цифры кода. "
            "После проверки я сразу удалю ваше сообщение."
        )

    @router.message(F.text.regexp(r"^\d{5,6}$"))
    async def login_code(message: Message) -> None:
        if not private_controller(message, database, settings) or not login.waiting_for_code:
            return
        code = message.text or ""
        await remove_sensitive_message(bot, message)
        try:
            result = await login.submit_code(code)
        except PhoneCodeInvalidError:
            await message.answer("Код не подошёл. Пришлите актуальный код ещё раз.")
            return
        except PhoneCodeExpiredError:
            await message.answer("Срок кода истёк. Отправьте /start, чтобы запросить новый.")
            return
        except Exception:
            logger.exception("Telegram code sign-in failed")
            await message.answer("Не удалось войти по коду. Отправьте /start и попробуйте снова.")
            return
        if result == "password_needed":
            await message.answer("Включён облачный пароль Telegram. Пришлите его одним сообщением — я тоже сразу удалю его.")
        elif result == "authorized":
            await message.answer("Аккаунт Ксюши подключён. Запускаю первую проверку групп.")
            asyncio.create_task(monitor.run_cycle(force_full_sync=True))

    @router.message(F.text)
    async def login_password(message: Message) -> None:
        if not private_controller(message, database, settings) or not login.waiting_for_password:
            return
        password = message.text or ""
        await remove_sensitive_message(bot, message)
        try:
            await login.submit_password(password)
        except PasswordHashInvalidError:
            await message.answer("Пароль не подошёл. Пришлите его ещё раз.")
            return
        except Exception:
            logger.exception("Telegram password sign-in failed")
            await message.answer("Не удалось завершить вход. Отправьте /start, чтобы начать заново.")
            return
        await message.answer("Аккаунт Ксюши подключён. Запускаю первую проверку групп.")
        asyncio.create_task(monitor.run_cycle(force_full_sync=True))

    @router.callback_query(F.data.startswith("exclude:"))
    async def exclude(callback: CallbackQuery) -> None:
        logger.info(
            "Exclude action received for CRM message %s in chat %s",
            callback.message.message_id if callback.message else None,
            callback.message.chat.id if callback.message else None,
        )
        target_chat_id = database.target_chat_id()
        if callback.message is None or callback.message.chat.id != target_chat_id:
            await callback.answer("Эта кнопка недоступна", show_alert=True)
            return
        try:
            chat_id = int((callback.data or "").split(":", 1)[1])
        except (IndexError, ValueError):
            await callback.answer("Некорректная команда", show_alert=True)
            return
        chat = database.exclude(chat_id)
        if chat is None:
            await callback.answer("Чат не найден", show_alert=True)
            return
        await callback.answer("Чат исключён из мониторинга")
        try:
            await callback.message.delete()
        except TelegramBadRequest as error:
            logger.warning("Could not delete excluded alert card %s: %s", callback.message.message_id, error.message)
        else:
            logger.info("Excluded alert card %s deleted", callback.message.message_id)

    @router.callback_query(F.data.startswith("done:"))
    async def done(callback: CallbackQuery) -> None:
        logger.info(
            "Done action received for CRM message %s in chat %s",
            callback.message.message_id if callback.message else None,
            callback.message.chat.id if callback.message else None,
        )
        target_chat_id = database.target_chat_id()
        if callback.message is None or callback.message.chat.id != target_chat_id:
            await callback.answer("Эта кнопка недоступна", show_alert=True)
            return
        try:
            chat_id = int((callback.data or "").split(":", 1)[1])
        except (IndexError, ValueError):
            await callback.answer("Некорректная команда", show_alert=True)
            return
        database.resolve(chat_id)
        await callback.answer("Уведомление закрыто")
        try:
            await callback.message.delete()
        except TelegramBadRequest as error:
            logger.warning("Could not delete resolved alert card %s: %s", callback.message.message_id, error.message)
        else:
            logger.info("Resolved alert card %s deleted", callback.message.message_id)

    @router.callback_query(F.data.startswith("find:"))
    async def find_chat(callback: CallbackQuery) -> None:
        try:
            chat_id = int((callback.data or "").split(":", 1)[1])
        except (IndexError, ValueError):
            await callback.answer("Некорректная команда", show_alert=True)
            return
        chat = database.get_chat(chat_id)
        if chat is None:
            await callback.answer("Чат не найден", show_alert=True)
            return
        await callback.answer(
            f"Это старая закрытая группа без поддерживаемой прямой ссылки. "
            f"Найдите её по названию: {chat.title}",
            show_alert=True,
        )

    @router.callback_query(F.data.startswith("test:"))
    async def test_card_action(callback: CallbackQuery) -> None:
        """Disposable test cards never modify CRM monitoring state."""
        await callback.answer("Тестовая кнопка сработала")
        if callback.message is None:
            return
        try:
            await callback.message.delete()
        except TelegramBadRequest:
            logger.warning("Could not delete test card %s", callback.message.message_id)

    return router


async def main() -> None:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = Settings.from_env()
    database = Database(settings.database_path)
    database.initialize()
    bot = Bot(settings.bot_token, default=DefaultBotProperties(parse_mode="HTML"))
    login = AccountLogin(settings)
    monitor = InactivityMonitor(settings, database, login, bot)
    dispatcher = Dispatcher()
    dispatcher.include_router(create_router(bot, database, settings, login, monitor))
    if os.getenv("REBUILD_ALERT_CARDS_ON_START") == "1" and database.get_setting("alert_cards_rebuilt_v3") != "done":
        await monitor.rebuild_alert_cards()
        database.set_setting("alert_cards_rebuilt_v3", "done")
    scan_task = asyncio.create_task(monitor.run_forever())
    legacy_task: asyncio.Task[None] | None = None
    if os.getenv("REPLACE_LEGACY_CARD_BUTTONS_ON_START") == "1" and database.get_setting("legacy_links_replaced_v1") != "done":
        async def replace_legacy_once() -> None:
            await monitor.replace_legacy_card_buttons()
            database.set_setting("legacy_links_replaced_v1", "done")

        legacy_task = asyncio.create_task(replace_legacy_once())
    try:
        await dispatcher.start_polling(bot, allowed_updates=dispatcher.resolve_used_update_types())
    finally:
        scan_task.cancel()
        if legacy_task:
            legacy_task.cancel()
        await login.disconnect()
        await bot.session.close()
        database.close()


if __name__ == "__main__":
    asyncio.run(main())
