from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from html import escape

from .domain import DraftBatch, ParsedItem, TransactionType
from .reports import ReportSummary

_CATEGORY_ICONS = {
    "Продукти": "🛒",
    "Кафе/ресторани": "☕",
    "Транспорт": "🚕",
    "Комунальні послуги": "🏠",
    "Здоров'я": "💊",
    "Одяг": "👕",
    "Розваги": "🎬",
    "Підписки": "🔁",
    "Зарплата": "💼",
    "Підробіток": "🧰",
    "Подарунок": "🎁",
    "Інше": "💳",
}


def format_amount(amount: Decimal, currency: str, *, signed: bool = False) -> str:
    normalized = amount.quantize(Decimal("0.01"))
    if normalized == normalized.to_integral():
        number = f"{normalized:,.0f}"
    else:
        number = f"{normalized:,.2f}"
    number = number.replace(",", " ")
    suffix = {"UAH": "грн", "USD": "USD", "EUR": "EUR"}.get(currency, currency)
    prefix = "+" if signed and amount > 0 else ""
    return f"{prefix}{number} {suffix}"


def _draft_line(item: ParsedItem, index: int | None = None) -> str:
    icon = _CATEGORY_ICONS.get(item.category, "💳")
    amount = (
        "сума не визначена"
        if item.amount is None
        else format_amount(item.amount, item.currency.value)
    )
    type_label = "дохід" if item.type == TransactionType.INCOME else "витрата"
    prefix = f"{index}. " if index is not None else ""
    return (
        f"{prefix}{icon} <b>{escape(item.description)}</b> — {amount}\n"
        f"   {type_label}, категорія «{escape(item.category)}»"
    )


def format_confirmation(draft: DraftBatch) -> str:
    if len(draft.items) == 1:
        body = _draft_line(draft.items[0])
    else:
        body = "\n\n".join(_draft_line(item, index) for index, item in enumerate(draft.items, 1))
    warning = ""
    if any(item.confidence.value == "low" for item in draft.items):
        warning = "\n\n⚠️ Я не цілком упевнений у розпізнаванні — перевір, будь ласка."
    return f"Я зрозумів так:\n\n{body}{warning}\n\nВсе вірно?"


def format_categories(expense: list[str], income: list[str]) -> str:
    expenses = "\n".join(f"• {escape(value)}" for value in expense)
    incomes = "\n".join(f"• {escape(value)}" for value in income)
    return f"<b>Категорії витрат</b>\n{expenses}\n\n<b>Категорії доходів</b>\n{incomes}"


def format_report(report: ReportSummary) -> str:
    period_label = "цей тиждень" if report.period == "week" else "цей місяць"
    lines = [f"📊 <b>Звіт за {period_label}</b>"]
    if not report.currencies:
        lines.append("\nПоки що немає операцій за цей період.")
    for currency in sorted(report.currencies):
        summary = report.currencies[currency]
        lines.extend(
            [
                f"\n<b>{currency}</b>",
                f"Доходи: {format_amount(summary.income, currency)}",
                f"Витрати: {format_amount(summary.expense, currency)}",
                f"Баланс: {format_amount(summary.balance, currency, signed=True)}",
            ]
        )
        if summary.expense_categories:
            lines.append("\nВитрати за категоріями:")
            for category, amount in sorted(
                summary.expense_categories.items(), key=lambda pair: pair[1], reverse=True
            ):
                lines.append(f"• {escape(category)} — {format_amount(amount, currency)}")
        if summary.income_categories:
            lines.append("\nДоходи за категоріями:")
            for category, amount in sorted(
                summary.income_categories.items(), key=lambda pair: pair[1], reverse=True
            ):
                lines.append(f"• {escape(category)} — {format_amount(amount, currency)}")
    if report.malformed_rows:
        lines.append(f"\n⚠️ Не враховано пошкоджених рядків: {report.malformed_rows}.")
    return "\n".join(lines)


def format_connected(spreadsheet_id: str) -> str:
    return (
        "✅ Таблицю підключено й перевірено.\n\n"
        f"ID: <code>{escape(spreadsheet_id)}</code>\n"
        "Тепер просто напиши, наприклад: <i>кава 50 грн</i>."
    )


def format_transaction_date(value: datetime) -> str:
    return value.strftime("%d.%m.%Y %H:%M")
