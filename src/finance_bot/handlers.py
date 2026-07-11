from __future__ import annotations

import logging
from html import escape

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from .config import Settings
from .domain import Currency, parse_manual_amount
from .formatters import (
    format_amount,
    format_categories,
    format_confirmation,
    format_report,
    format_transaction_date,
)
from .keyboards import (
    category_actions_keyboard,
    category_kind_keyboard,
    category_manage_keyboard,
    confirmation_keyboard,
    duplicate_keyboard,
    item_picker_keyboard,
    report_keyboard,
    transaction_category_keyboard,
)
from .service import ConfirmKind, FinanceService, IngestKind, IngestOutcome
from .states import CategoryStates, TransactionStates
from .table_image import generate_table_image_from_bytes

logger = logging.getLogger(__name__)


def _user_id(message_or_query: Message | CallbackQuery) -> int:
    if message_or_query.from_user is None:
        raise RuntimeError("Telegram update has no user")
    return message_or_query.from_user.id


def _currency_from_text(text: str) -> Currency | None:
    lowered = text.casefold()
    if any(token in lowered for token in ("usd", "$", "дол")):
        return Currency.USD
    if any(token in lowered for token in ("eur", "€", "євро")):
        return Currency.EUR
    if any(token in lowered for token in ("uah", "грн", "₴")):
        return Currency.UAH
    return None


async def _send_ingest_outcome(
    message: Message,
    state: FSMContext,
    outcome: IngestOutcome,
) -> None:
    if outcome.kind == IngestKind.NOT_FINANCIAL:
        await message.answer(
            "Не побачив тут фінансової операції. Напиши, наприклад: "
            "<i>кава 50 грн</i> або <i>отримав 1200 за проєкт</i>."
        )
        return
    if outcome.kind == IngestKind.UNREADABLE:
        await message.answer(
            "Не вдалося надійно прочитати операцію. Для чека спробуй чіткіше фото, "
            "або просто введи назву й суму текстом."
        )
        return
    if outcome.draft is None:
        await message.answer("Не вдалося створити чернетку операції. Спробуй ще раз.")
        return
    if outcome.kind == IngestKind.NEEDS_AMOUNT:
        await state.set_state(TransactionStates.waiting_amount)
        await state.set_data(
            {"draft_token": outcome.draft.token, "item_index": outcome.missing_index or 0}
        )
        description = outcome.draft.items[outcome.missing_index or 0].description
        await message.answer(
            f"Не зміг упевнено визначити суму для «{escape(description)}». "
            "Введи її вручну, наприклад: <code>1250,50 грн</code>."
        )
        return
    await message.answer(
        format_confirmation(outcome.draft),
        reply_markup=confirmation_keyboard(outcome.draft.token),
    )


def create_router(service: FinanceService, settings: Settings) -> Router:
    router = Router(name="finance")

    @router.message(CommandStart())
    async def start(message: Message, state: FSMContext) -> None:
        await state.clear()
        await service.state.ensure_user(_user_id(message))
        await message.answer(
            "Привіт! Я записую особисті фінанси без форм і меню.\n\n"
            "Просто напиши <i>кава 50 грн</i> або надішли фото чека. "
            "Я покажу, що зрозумів, і запишу дані лише після підтвердження.\n\n"
            "Команди:\n"
            "/table — фото-таблиця витрат і доходів\n"
            "/report — текстовий звіт за тиждень або місяць\n"
            "/categories — керування категоріями\n"
            "/undo — скасувати останню операцію"
        )

    @router.message(Command("cancel"))
    async def cancel_state(message: Message, state: FSMContext) -> None:
        data = await state.get_data()
        token = data.get("draft_token")
        if token:
            await service.cancel_draft(str(token), _user_id(message))
        await state.clear()
        await message.answer("Скасовано.")

    @router.message(Command("report"))
    async def report(message: Message) -> None:
        try:
            summary = await service.report(_user_id(message), "week")
        except Exception:
            logger.exception("Could not build report", extra={"user_id": _user_id(message)})
            await message.answer("Не вдалося згенерувати звіт. Спробуй трохи пізніше.")
        else:
            await message.answer(format_report(summary), reply_markup=report_keyboard("week"))

    @router.message(Command("table"))
    async def table(message: Message, bot: Bot) -> None:
        progress = await message.answer("Генерую таблицю…")
        try:
            transactions, start, end = await service.get_transactions(_user_id(message), "week")
            png_bytes = generate_table_image_from_bytes(
                transactions, period="week", start=start, end=end
            )
            await bot.send_photo(
                message.chat.id,
                photo=png_bytes,
                caption="Таблиця за цей тиждень",
                reply_markup=_table_keyboard("week"),
            )
            await progress.delete()
        except Exception:
            logger.exception("Could not generate table", extra={"user_id": _user_id(message)})
            try:
                await progress.edit_text("Не вдалося згенерувати таблицю. Спробуй трохи пізніше.")
            except Exception:
                pass
                
    @router.message(Command("categories"))
    async def categories(message: Message, state: FSMContext) -> None:
        await state.clear()
        expense, income = await service.get_categories(_user_id(message))
        await message.answer(
            format_categories(expense, income),
            reply_markup=category_actions_keyboard(),
        )

    @router.message(Command("undo"))
    async def undo(message: Message) -> None:
        try:
            transaction = await service.undo(_user_id(message))
        except Exception:
            logger.exception("Could not undo transaction", extra={"user_id": _user_id(message)})
            await message.answer("Не вдалося видалити запис. Спробуй ще раз.")
        else:
            if transaction is None:
                await message.answer("Немає записів, які можна скасувати.")
            else:
                await message.answer(
                    "↩️ Видалено: "
                    f"<b>{escape(transaction.description)}</b> — "
                    f"{format_amount(transaction.amount, transaction.currency.value)} "
                    f"({format_transaction_date(transaction.occurred_at)})."
                )

    @router.callback_query(F.data.startswith("report:"))
    async def report_callback(query: CallbackQuery) -> None:
        await query.answer()
        period = (query.data or "report:week").split(":", 1)[1]
        if period not in {"week", "month"} or not isinstance(query.message, Message):
            return
        try:
            summary = await service.report(_user_id(query), period)  # type: ignore[arg-type]
        except Exception:
            logger.exception("Could not refresh report", extra={"user_id": _user_id(query)})
            await query.answer("Не вдалося оновити звіт", show_alert=True)
        else:
            await query.message.edit_text(
                format_report(summary),
                reply_markup=report_keyboard(period),
            )

    @router.callback_query(F.data.startswith("tx:"))
    async def transaction_callback(query: CallbackQuery) -> None:
        data = (query.data or "").split(":")
        if len(data) < 3:
            await query.answer("Некоректна кнопка", show_alert=True)
            return
        action, token = data[1], data[2]
        user_id = _user_id(query)

        if action in {"ok", "force"}:
            await query.answer("Записую…")
            try:
                result = await service.confirm(
                    token,
                    user_id,
                    force_duplicate=action == "force",
                )
            except Exception:
                logger.exception("Could not commit draft", extra={"user_id": user_id})
                await query.answer("Помилка запису. Спробуй ще раз.", show_alert=True)
                return
            if not isinstance(query.message, Message):
                return
            if result.kind == ConfirmKind.DUPLICATE and result.draft:
                await query.message.edit_text(
                    format_confirmation(result.draft)
                    + "\n\n⚠️ Дуже схожа операція вже була записана щойно. "
                    "Записати ще одну?",
                    reply_markup=duplicate_keyboard(token),
                )
            elif result.kind == ConfirmKind.SAVED:
                count = len(result.transactions or [])
                noun = "операцію" if count == 1 else f"операцій: {count}"
                await query.message.edit_text(f"✅ Записано {noun}.")
            elif result.kind == ConfirmKind.NEEDS_AMOUNT:
                await query.answer("Спочатку потрібно вказати суму", show_alert=True)
            else:
                await query.answer("Ця операція вже оброблена або застаріла", show_alert=True)
            return

        if action == "no":
            cancelled = await service.cancel_draft(token, user_id)
            await query.answer("Скасовано" if cancelled else "Операція вже недоступна")
            if cancelled and isinstance(query.message, Message):
                await query.message.edit_text("❌ Операцію скасовано.")
            return

        if action == "cat":
            await query.answer()
            draft = await service.state.get_draft(token, user_id)
            if draft is None or not isinstance(query.message, Message):
                await query.answer("Чернетка вже недоступна", show_alert=True)
                return
            if len(draft.items) > 1:
                await query.message.edit_reply_markup(
                    reply_markup=item_picker_keyboard(
                        token, [item.description for item in draft.items]
                    )
                )
            else:
                selected = await service.categories_for_item(token, user_id, 0)
                if selected is None:
                    await query.answer("Чернетка вже недоступна", show_alert=True)
                    return
                _, available = selected
                await query.message.edit_reply_markup(
                    reply_markup=transaction_category_keyboard(token, 0, available)
                )
            return

        if action == "item" and len(data) == 4:
            await query.answer()
            index = int(data[3])
            selected = await service.categories_for_item(token, user_id, index)
            if selected and isinstance(query.message, Message):
                _, available = selected
                await query.message.edit_reply_markup(
                    reply_markup=transaction_category_keyboard(token, index, available)
                )
            return

        if action == "setcat" and len(data) == 5:
            await query.answer("Категорію змінено")
            item_index, category_index = int(data[3]), int(data[4])
            selected = await service.categories_for_item(token, user_id, item_index)
            if selected is None or not 0 <= category_index < len(selected[1]):
                await query.answer("Категорія вже недоступна", show_alert=True)
                return
            updated = await service.change_category(
                token,
                user_id,
                item_index,
                selected[1][category_index],
            )
            if updated and isinstance(query.message, Message):
                await query.message.edit_text(
                    format_confirmation(updated),
                    reply_markup=confirmation_keyboard(token),
                )
            return

        if action == "back":
            await query.answer()
            draft = await service.state.get_draft(token, user_id)
            if draft and isinstance(query.message, Message):
                await query.message.edit_text(
                    format_confirmation(draft),
                    reply_markup=confirmation_keyboard(token),
                )
            return

        await query.answer("Ця кнопка вже недоступна", show_alert=True)

    @router.callback_query(F.data.startswith("cats:"))
    async def categories_callback(query: CallbackQuery, state: FSMContext) -> None:
        data = (query.data or "").split(":")
        action = data[1] if len(data) > 1 else ""
        user_id = _user_id(query)
        await query.answer()
        if not isinstance(query.message, Message):
            return

        if action == "home":
            expense, income = await service.get_categories(user_id)
            await query.message.edit_text(
                format_categories(expense, income), reply_markup=category_actions_keyboard()
            )
            return
        if action == "add" and len(data) == 3:
            await state.set_state(CategoryStates.waiting_new_name)
            await state.set_data({"category_kind": data[2]})
            label = "витратної" if data[2] == "e" else "дохідної"
            await query.message.answer(f"Надішли назву нової {label} категорії.")
            return
        if action == "choose" and len(data) == 3:
            await query.message.edit_reply_markup(reply_markup=category_kind_keyboard(data[2]))
            return
        if action == "list" and len(data) == 4:
            manage_action, kind = data[2], data[3]
            expense, income = await service.get_categories(user_id)
            available = expense if kind == "e" else income
            await query.message.edit_reply_markup(
                reply_markup=category_manage_keyboard(manage_action, kind, available)
            )
            return
        if action == "rn" and len(data) == 4:
            await state.set_state(CategoryStates.waiting_rename)
            await state.set_data({"category_kind": data[2], "category_index": int(data[3])})
            await query.message.answer("Надішли нову назву категорії.")
            return
        if action == "rm" and len(data) == 4:
            try:
                removed = await service.remove_category(user_id, data[2], int(data[3]))
            except ValueError as error:
                await query.answer(str(error), show_alert=True)
            else:
                expense, income = await service.get_categories(user_id)
                await query.message.edit_text(
                    f"🗑 Видалено «{escape(removed)}».\n\n" + format_categories(expense, income),
                    reply_markup=category_actions_keyboard(),
                )

    @router.message(CategoryStates.waiting_new_name, F.text)
    async def category_add_name(message: Message, state: FSMContext) -> None:
        data = await state.get_data()
        try:
            await service.add_category(
                _user_id(message), str(data.get("category_kind", "")), message.text or ""
            )
        except ValueError as error:
            await message.answer(f"Не вдалося додати: {escape(str(error))}. Спробуй іншу назву.")
            return
        await state.clear()
        expense, income = await service.get_categories(_user_id(message))
        await message.answer(
            "✅ Категорію додано.\n\n" + format_categories(expense, income),
            reply_markup=category_actions_keyboard(),
        )

    @router.message(CategoryStates.waiting_rename, F.text)
    async def category_rename_name(message: Message, state: FSMContext) -> None:
        data = await state.get_data()
        try:
            await service.rename_category(
                _user_id(message),
                str(data.get("category_kind", "")),
                int(data.get("category_index", -1)),
                message.text or "",
            )
        except ValueError as error:
            await message.answer(f"Не вдалося перейменувати: {escape(str(error))}.")
            return
        await state.clear()
        expense, income = await service.get_categories(_user_id(message))
        await message.answer(
            "✅ Категорію перейменовано.\n\n" + format_categories(expense, income),
            reply_markup=category_actions_keyboard(),
        )

    @router.message(TransactionStates.waiting_amount, F.text)
    async def manual_amount(message: Message, state: FSMContext) -> None:
        amount = parse_manual_amount(message.text or "")
        if amount is None:
            await message.answer("Не впізнав суму. Спробуй формат <code>1250,50 грн</code>.")
            return
        data = await state.get_data()
        outcome = await service.fill_amount(
            str(data.get("draft_token", "")),
            _user_id(message),
            int(data.get("item_index", 0)),
            amount,
            currency=_currency_from_text(message.text or ""),
        )
        if outcome is None:
            await state.clear()
            await message.answer("Ця чернетка вже застаріла. Надішли операцію ще раз.")
            return
        if outcome.next_missing_index is not None:
            await state.update_data(item_index=outcome.next_missing_index)
            description = outcome.draft.items[outcome.next_missing_index].description
            await message.answer(f"Тепер введи суму для «{escape(description)}».")
            return
        await state.clear()
        await message.answer(
            format_confirmation(outcome.draft),
            reply_markup=confirmation_keyboard(outcome.draft.token),
        )

    @router.message(F.photo)
    async def photo(message: Message, bot: Bot, state: FSMContext) -> None:
        await state.clear()
        largest = message.photo[-1]
        if largest.file_size and largest.file_size > settings.max_image_bytes:
            await message.answer(
                f"Фото завелике. Максимум — {settings.max_image_bytes // 1024 // 1024} МБ."
            )
            return
        progress = await message.answer("Читаю чек…")
        try:
            buffer = await bot.download(largest)
            if buffer is None:
                raise RuntimeError("Telegram returned no photo data")
            image = buffer.getvalue()
            if len(image) > settings.max_image_bytes:
                raise ValueError("image is too large")
            outcome = await service.ingest_photo(_user_id(message), image)
        except ValueError:
            await progress.edit_text("Фото завелике або має непідтримуваний формат.")
        except Exception:
            logger.exception("Could not process receipt", extra={"user_id": _user_id(message)})
            await progress.edit_text(
                "Не вдалося прочитати чек. Спробуй ще раз або введи суму текстом."
            )
        else:
            await progress.delete()
            await _send_ingest_outcome(message, state, outcome)

    @router.message(F.text)
    async def text_transaction(message: Message, state: FSMContext) -> None:
        await state.clear()
        try:
            outcome = await service.ingest_text(_user_id(message), message.text or "")
        except Exception:
            logger.exception("Could not parse transaction", extra={"user_id": _user_id(message)})
            await message.answer("Не вдалося розпізнати операцію. Спробуй ще раз трохи пізніше.")
        else:
            await _send_ingest_outcome(message, state, outcome)

    @router.callback_query(F.data.startswith("table:"))
    async def table_callback(query: CallbackQuery, bot: Bot) -> None:
        await query.answer()
        period = (query.data or "table:week").split(":", 1)[1]
        if period not in {"week", "month"} or not isinstance(query.message, Message):
            return
        try:
            transactions, start, end = await service.get_transactions(
                _user_id(query), period  # type: ignore[arg-type]
            )
            png_bytes = generate_table_image_from_bytes(
                transactions, period=period, start=start, end=end  # type: ignore[arg-type]
            )
            caption = "Таблиця за цей тиждень" if period == "week" else "Таблиця за цей місяць"
            if isinstance(query.message, Message) and query.message.photo:
                await bot.send_photo(
                    query.chat.id,
                    photo=png_bytes,
                    caption=caption,
                    reply_markup=_table_keyboard(period),
                )
                await query.message.delete()
            else:
                await query.message.answer_photo(
                    photo=png_bytes,
                    caption=caption,
                    reply_markup=_table_keyboard(period),
                )
        except Exception:
            logger.exception("Could not refresh table", extra={"user_id": _user_id(query)})
            await query.answer("Не вдалося оновити таблицю", show_alert=True)

    @router.message()
    async def unsupported(message: Message) -> None:
        await message.answer("Надішли текст операції або фото чека.")

    return router


def _table_keyboard(active: str = "week") -> InlineKeyboardMarkup:
    week = "Тиждень" if active != "week" else "Тиждень"
    month = "Місяць" if active != "month" else "Місяць"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=week, callback_data="table:week"),
                InlineKeyboardButton(text=month, callback_data="table:month"),
            ]
        ]
    )