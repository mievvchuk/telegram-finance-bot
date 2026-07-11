from __future__ import annotations

from collections.abc import Sequence

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder


def confirmation_keyboard(token: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Все вірно", callback_data=f"tx:ok:{token}")],
            [InlineKeyboardButton(text="✏️ Змінити категорію", callback_data=f"tx:cat:{token}")],
            [InlineKeyboardButton(text="❌ Скасувати", callback_data=f"tx:no:{token}")],
        ]
    )


def duplicate_keyboard(token: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Записати ще раз", callback_data=f"tx:force:{token}")],
            [InlineKeyboardButton(text="❌ Скасувати", callback_data=f"tx:no:{token}")],
        ]
    )


def item_picker_keyboard(token: str, descriptions: Sequence[str]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for index, description in enumerate(descriptions):
        label = description.strip()[:35] or f"Операція {index + 1}"
        builder.button(text=f"{index + 1}. {label}", callback_data=f"tx:item:{token}:{index}")
    builder.adjust(1)
    return builder.as_markup()


def transaction_category_keyboard(
    token: str,
    item_index: int,
    categories: Sequence[str],
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for category_index, category in enumerate(categories):
        builder.button(
            text=category,
            callback_data=f"tx:setcat:{token}:{item_index}:{category_index}",
        )
    builder.button(text="↩️ Назад", callback_data=f"tx:back:{token}")
    builder.adjust(2)
    return builder.as_markup()


def report_keyboard(active: str = "week") -> InlineKeyboardMarkup:
    week = "✓ Цей тиждень" if active == "week" else "Цей тиждень"
    month = "✓ Цей місяць" if active == "month" else "Цей місяць"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=week, callback_data="report:week"),
                InlineKeyboardButton(text=month, callback_data="report:month"),
            ]
        ]
    )


def category_actions_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="➕ Витратну", callback_data="cats:add:e"),
                InlineKeyboardButton(text="➕ Дохідну", callback_data="cats:add:i"),
            ],
            [
                InlineKeyboardButton(text="✏️ Перейменувати", callback_data="cats:choose:rn"),
                InlineKeyboardButton(text="🗑 Видалити", callback_data="cats:choose:rm"),
            ],
        ]
    )


def category_kind_keyboard(action: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Витрати", callback_data=f"cats:list:{action}:e"),
                InlineKeyboardButton(text="Доходи", callback_data=f"cats:list:{action}:i"),
            ],
            [InlineKeyboardButton(text="↩️ Назад", callback_data="cats:home")],
        ]
    )


def category_manage_keyboard(
    action: str,
    kind: str,
    categories: Sequence[str],
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for index, category in enumerate(categories):
        builder.button(text=category, callback_data=f"cats:{action}:{kind}:{index}")
    builder.button(text="↩️ Назад", callback_data=f"cats:choose:{action}")
    builder.adjust(2)
    return builder.as_markup()
