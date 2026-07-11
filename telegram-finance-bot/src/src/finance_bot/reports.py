from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from decimal import Decimal
from typing import Literal

from .domain import Transaction, TransactionType

ReportPeriod = Literal["week", "month"]


@dataclass(slots=True)
class CurrencySummary:
    income: Decimal = Decimal("0")
    expense: Decimal = Decimal("0")
    income_categories: dict[str, Decimal] = field(default_factory=dict)
    expense_categories: dict[str, Decimal] = field(default_factory=dict)

    @property
    def balance(self) -> Decimal:
        return self.income - self.expense


@dataclass(slots=True)
class ReportSummary:
    period: ReportPeriod
    start: datetime
    end: datetime
    currencies: dict[str, CurrencySummary]
    transaction_count: int
    malformed_rows: int = 0


def report_bounds(now: datetime, period: ReportPeriod) -> tuple[datetime, datetime]:
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    if period == "week":
        start_date = (now - timedelta(days=now.weekday())).date()
    elif period == "month":
        start_date = now.date().replace(day=1)
    else:
        raise ValueError(f"Unsupported report period: {period}")
    return datetime.combine(start_date, time.min, tzinfo=now.tzinfo), now


def build_report(
    transactions: list[Transaction],
    *,
    period: ReportPeriod,
    start: datetime,
    end: datetime,
    malformed_rows: int = 0,
) -> ReportSummary:
    raw: dict[str, dict[str, object]] = defaultdict(
        lambda: {
            "income": Decimal("0"),
            "expense": Decimal("0"),
            "income_categories": defaultdict(Decimal),
            "expense_categories": defaultdict(Decimal),
        }
    )

    included = 0
    for transaction in transactions:
        if not start <= transaction.occurred_at <= end:
            continue
        currency = transaction.currency.value
        entry = raw[currency]
        if transaction.type == TransactionType.INCOME:
            entry["income"] += transaction.amount  # type: ignore[operator]
            entry["income_categories"][transaction.category] += transaction.amount  # type: ignore[index]
        else:
            entry["expense"] += transaction.amount  # type: ignore[operator]
            entry["expense_categories"][transaction.category] += transaction.amount  # type: ignore[index]
        included += 1

    currencies: dict[str, CurrencySummary] = {}
    for currency, entry in raw.items():
        currencies[currency] = CurrencySummary(
            income=entry["income"],  # type: ignore[arg-type]
            expense=entry["expense"],  # type: ignore[arg-type]
            income_categories=dict(entry["income_categories"]),  # type: ignore[arg-type]
            expense_categories=dict(entry["expense_categories"]),  # type: ignore[arg-type]
        )

    return ReportSummary(
        period=period,
        start=start,
        end=end,
        currencies=currencies,
        transaction_count=included,
        malformed_rows=malformed_rows,
    )
