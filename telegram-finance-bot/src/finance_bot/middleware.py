from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject


class AccessMiddleware(BaseMiddleware):
    def __init__(self, allowed_user_ids: frozenset[int]) -> None:
        self._allowed_user_ids = allowed_user_ids

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = data.get("event_from_user")
        if user is None:
            return None
        if self._allowed_user_ids and user.id not in self._allowed_user_ids:
            await self._deny(event, "Цей бот приватний. Ваш Telegram ID не додано до allowlist.")
            return None

        chat = data.get("event_chat")
        if chat is not None and chat.type != "private":
            await self._deny(
                event,
                "З міркувань приватності фінансовий бот працює лише в особистому чаті.",
            )
            return None
        return await handler(event, data)

    @staticmethod
    async def _deny(event: TelegramObject, text: str) -> None:
        if isinstance(event, Message):
            await event.answer(text)
        elif isinstance(event, CallbackQuery):
            await event.answer(text, show_alert=True)
