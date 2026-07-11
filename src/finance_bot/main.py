from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand

from .config import Settings
from .db import SQLiteState
from .handlers import create_router
from .middleware import AccessMiddleware
from .parser import GlmFinanceParser
from .service import FinanceService

logger = logging.getLogger(__name__)


async def run(settings: Settings | None = None) -> None:
    config = settings or Settings()
    logging.basicConfig(
        level=config.log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    state = SQLiteState(config.database_path)
    parser = GlmFinanceParser(
        config.zai_api_key.get_secret_value(),
        base_url=config.zai_base_url,
        text_model=config.zai_text_model,
        vision_model=config.zai_vision_model,
        timeout_seconds=config.zai_timeout_seconds,
    )
    service = FinanceService(parser=parser, state=state, settings=config)
    bot = Bot(
        token=config.telegram_bot_token.get_secret_value(),
        default=DefaultBotProperties(
            parse_mode=ParseMode.HTML,
            link_preview_is_disabled=True,
        ),
    )
    dispatcher = Dispatcher(storage=MemoryStorage())
    access = AccessMiddleware(config.allowed_user_ids)
    dispatcher.message.outer_middleware(access)
    dispatcher.callback_query.outer_middleware(access)
    dispatcher.include_router(create_router(service, config))

    await state.initialize()
    await bot.set_my_commands(
        [
            BotCommand(command="start", description="Коротка інструкція"),
            BotCommand(command="table", description="Таблиця витрат і доходів (фото)"),
            BotCommand(command="report", description="Звіт за тиждень або місяць"),
            BotCommand(command="categories", description="Перегляд і зміна категорій"),
            BotCommand(command="undo", description="Видалити останню операцію"),
            BotCommand(command="cancel", description="Скасувати поточну дію"),
        ]
    )
    await bot.delete_webhook(drop_pending_updates=False)
    logger.info("Starting long polling")
    try:
        await dispatcher.start_polling(
            bot,
            allowed_updates=dispatcher.resolve_used_update_types(),
        )
    finally:
        await parser.close()
        await state.close()
        await bot.session.close()


def cli() -> None:
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    cli()