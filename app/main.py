from __future__ import annotations

import asyncio
import logging
import signal
from contextlib import suppress

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand
from aiogram.webhook.aiohttp_server import SimpleRequestHandler
from aiohttp import web

from app.config import Settings, load_settings
from app.database import async_session_factory, close_db, create_db_engine, init_db
from app.handlers import common, employee, owner_finance, owner_staff
from app.middlewares import AppContextMiddleware
from app.services.scheduler import reminder_loop
from app.services.tunnel import miniapp_runtime, restore_telegram_menu_button, start_tunnel_supervisor
from app.webapp import create_webapp

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger(__name__)


def configure_webhook_route(
    webapp: web.Application,
    dp: Dispatcher,
    bot: Bot,
    settings: Settings,
    *,
    handle_in_background: bool = True,
) -> SimpleRequestHandler:
    """Реєструє HTTP-маршрут для прийому Telegram Webhook на існуючому aiohttp-сервері."""
    handler = SimpleRequestHandler(
        dispatcher=dp,
        bot=bot,
        handle_in_background=handle_in_background,
        secret_token=settings.webhook_secret,
    )
    handler.register(webapp, path=settings.webhook_path)
    return handler


async def _wait_for_shutdown_signal() -> None:
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with suppress(NotImplementedError, RuntimeError):
            loop.add_signal_handler(sig, stop_event.set)
    try:
        await stop_event.wait()
    finally:
        for sig in (signal.SIGINT, signal.SIGTERM):
            with suppress(NotImplementedError, RuntimeError):
                loop.remove_signal_handler(sig)


async def main() -> None:
    settings = load_settings()
    engine = create_db_engine(settings.db_path, database_url=settings.database_url)
    session_maker = async_session_factory(engine)
    await init_db(engine, session_maker)

    bot = Bot(
        token=settings.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher(storage=MemoryStorage())

    middleware = AppContextMiddleware(session_maker=session_maker, settings=settings)
    dp.message.outer_middleware(middleware)
    dp.callback_query.outer_middleware(middleware)

    dp.include_router(common.start_router)
    dp.include_router(employee.router)
    dp.include_router(owner_staff.router)
    dp.include_router(owner_finance.router)
    dp.include_router(common.fallback_router)

    webapp = create_webapp(session_maker=session_maker, bot=bot, settings=settings)
    if settings.bot_mode == "webhook":
        configure_webhook_route(webapp, dp, bot, settings)

    runner = web.AppRunner(webapp)
    await runner.setup()
    site = web.TCPSite(runner, host=settings.miniapp_host, port=settings.miniapp_port)
    await site.start()
    logger.info(
        "HTTP-сервер Mini App запущено на http://%s:%s (режим: %s)",
        settings.miniapp_host,
        settings.miniapp_port,
        settings.bot_mode,
    )

    scheduler_task: asyncio.Task[None] | None = None
    tunnel_task: asyncio.Task[None] | None = None
    db_label = "PostgreSQL (DATABASE_URL)" if settings.database_url else f"SQLite ({settings.db_path})"

    try:
        await bot.set_my_commands(
            [
                BotCommand(command="start", description="Головне меню"),
                BotCommand(command="menu", description="Показати кнопки меню"),
                BotCommand(command="app", description="Відкрити Mini App кабінет"),
                BotCommand(command="cancel", description="Скасувати поточну дію"),
            ]
        )
        await restore_telegram_menu_button(bot)

        scheduler_task = asyncio.create_task(reminder_loop(session_maker, bot, settings))

        if settings.bot_mode == "webhook":
            miniapp_runtime.url = settings.miniapp_url
            miniapp_runtime.status = "online"
            miniapp_runtime.provider = "cloud"
            miniapp_runtime.error = None

            webhook_url = f"{settings.miniapp_url}{settings.webhook_path}"
            await bot.set_webhook(
                webhook_url,
                secret_token=settings.webhook_secret,
                drop_pending_updates=False,
            )
            logger.info("Webhook активовано: %s | База даних: %s", webhook_url, db_label)
            await _wait_for_shutdown_signal()
        else:
            await bot.delete_webhook(drop_pending_updates=False)
            tunnel_task = asyncio.create_task(start_tunnel_supervisor(bot, settings))
            logger.info("Бот запущено у polling-режимі (Чат + Mini App)! База даних: %s", db_label)
            await dp.start_polling(bot)
    finally:
        for task in (scheduler_task, tunnel_task):
            if task is not None:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
        await runner.cleanup()
        await bot.session.close()
        await close_db(engine)


if __name__ == "__main__":
    asyncio.run(main())
