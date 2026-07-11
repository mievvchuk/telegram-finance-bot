from __future__ import annotations

import asyncio
import secrets
import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Any
from zoneinfo import ZoneInfo

from .config import Settings
from .db import SQLiteState
from .domain import (
    Confidence,
    DraftBatch,
    DraftStatus,
    ParseStatus,
    SourceType,
    Transaction,
    TransactionType,
    extract_google_sheet_id,
)
from .reports import ReportPeriod, ReportSummary, build_report, report_bounds
from .sheets import GoogleSheetsRepository


class NotConnectedError(RuntimeError):
    pass


class IngestKind(StrEnum):
    DRAFT = "draft"
    NEEDS_AMOUNT = "needs_amount"
    NOT_FINANCIAL = "not_financial"
    UNREADABLE = "unreadable"


class ConfirmKind(StrEnum):
    SAVED = "saved"
    DUPLICATE = "duplicate"
    UNAVAILABLE = "unavailable"
    NEEDS_AMOUNT = "needs_amount"


@dataclass(slots=True)
class IngestOutcome:
    kind: IngestKind
    draft: DraftBatch | None = None
    missing_index: int | None = None
    issue: str | None = None


@dataclass(slots=True)
class ConfirmOutcome:
    kind: ConfirmKind
    draft: DraftBatch | None = None
    transactions: list[Transaction] | None = None
    duplicates: list[Transaction] | None = None


@dataclass(slots=True)
class AmountOutcome:
    draft: DraftBatch
    next_missing_index: int | None


def _category_kind(value: str) -> TransactionType:
    if value in {"e", "expense"}:
        return TransactionType.EXPENSE
    if value in {"i", "income"}:
        return TransactionType.INCOME
    raise ValueError("Невідомий тип категорії")


def _validate_category_name(value: str) -> str:
    cleaned = " ".join(value.split()).strip()
    if not 1 <= len(cleaned) <= 40:
        raise ValueError("Назва категорії має містити від 1 до 40 символів")
    if any(character in cleaned for character in "\n\r\t"):
        raise ValueError("Назва категорії має бути в одному рядку")
    return cleaned


class FinanceService:
    def __init__(
        self,
        *,
        parser: Any,
        sheets: GoogleSheetsRepository,
        state: SQLiteState,
        settings: Settings,
    ) -> None:
        self.parser = parser
        self.sheets = sheets
        self.state = state
        self.settings = settings
        self.timezone = ZoneInfo(settings.app_timezone)
        self._user_locks: defaultdict[int, asyncio.Lock] = defaultdict(asyncio.Lock)

    @property
    def service_account_email(self) -> str | None:
        return self.sheets.service_account_email

    async def get_sheet_id(self, telegram_user_id: int) -> str | None:
        await self.state.ensure_user(telegram_user_id)
        linked = await self.state.get_sheet_id(telegram_user_id)
        return linked or self.settings.default_google_sheet_id

    async def require_sheet_id(self, telegram_user_id: int) -> str:
        sheet_id = await self.get_sheet_id(telegram_user_id)
        if not sheet_id:
            raise NotConnectedError("Google Таблицю ще не підключено")
        return sheet_id

    async def connect_sheet(self, telegram_user_id: int, url_or_id: str) -> str:
        sheet_id = extract_google_sheet_id(url_or_id)
        await self.sheets.ensure_schema(sheet_id)
        await self.state.connect_sheet(telegram_user_id, sheet_id)
        return sheet_id

    async def get_categories(self, telegram_user_id: int) -> tuple[list[str], list[str]]:
        return await self.state.get_categories(telegram_user_id)

    async def ingest_text(self, telegram_user_id: int, text: str) -> IngestOutcome:
        await self.require_sheet_id(telegram_user_id)
        expense, income = await self.state.get_categories(telegram_user_id)
        now = datetime.now(self.timezone)
        parsed = await self.parser.parse_text(text, expense, income, now)
        return await self._create_draft(
            telegram_user_id,
            parsed,
            SourceType.TEXT,
            original_text=text,
        )

    async def ingest_photo(
        self,
        telegram_user_id: int,
        image_bytes: bytes,
        mime_type: str = "image/jpeg",
    ) -> IngestOutcome:
        await self.require_sheet_id(telegram_user_id)
        expense, income = await self.state.get_categories(telegram_user_id)
        now = datetime.now(self.timezone)
        parsed = await self.parser.parse_receipt(
            image_bytes,
            mime_type,
            expense,
            income,
            now,
        )
        return await self._create_draft(
            telegram_user_id,
            parsed,
            SourceType.PHOTO,
            original_text=None,
        )

    async def _create_draft(
        self,
        telegram_user_id: int,
        parsed: Any,
        source: SourceType,
        *,
        original_text: str | None,
    ) -> IngestOutcome:
        if parsed.status == ParseStatus.NOT_FINANCIAL:
            return IngestOutcome(IngestKind.NOT_FINANCIAL, issue=parsed.issue)
        if parsed.status == ParseStatus.UNREADABLE or not parsed.items:
            return IngestOutcome(IngestKind.UNREADABLE, issue=parsed.issue)

        missing_index = next(
            (index for index, item in enumerate(parsed.items) if item.amount is None),
            None,
        )
        status = DraftStatus.NEEDS_AMOUNT if missing_index is not None else DraftStatus.PENDING
        created_at = datetime.now(UTC)
        draft = DraftBatch(
            token=secrets.token_urlsafe(6),
            telegram_user_id=telegram_user_id,
            items=parsed.items,
            source=source,
            status=status,
            original_text=original_text,
            created_at=created_at,
            expires_at=created_at + timedelta(seconds=self.settings.draft_ttl_seconds),
        )
        await self.state.create_draft(draft)
        if missing_index is not None:
            return IngestOutcome(IngestKind.NEEDS_AMOUNT, draft, missing_index, parsed.issue)
        return IngestOutcome(IngestKind.DRAFT, draft=draft, issue=parsed.issue)

    async def fill_amount(
        self,
        token: str,
        telegram_user_id: int,
        item_index: int,
        amount: Decimal,
        *,
        currency: Any | None = None,
    ) -> AmountOutcome | None:
        draft = await self.state.get_draft(token, telegram_user_id)
        if draft is None or draft.status != DraftStatus.NEEDS_AMOUNT:
            return None
        if not 0 <= item_index < len(draft.items):
            return None
        if amount <= 0 or amount > Decimal("1000000000"):
            raise ValueError("Сума має бути більшою за 0 і не більшою за 1 000 000 000")

        item = draft.items[item_index]
        updates: dict[str, Any] = {"amount": amount, "confidence": Confidence.HIGH}
        if currency is not None:
            updates["currency"] = currency
        items = list(draft.items)
        items[item_index] = item.model_copy(update=updates)
        next_missing = next(
            (index for index, candidate in enumerate(items) if candidate.amount is None),
            None,
        )
        new_status = DraftStatus.NEEDS_AMOUNT if next_missing is not None else DraftStatus.PENDING
        updated = draft.model_copy(update={"items": items, "status": new_status})
        await self.state.save_draft(updated)
        return AmountOutcome(updated, next_missing)

    async def categories_for_item(
        self,
        token: str,
        telegram_user_id: int,
        item_index: int,
    ) -> tuple[DraftBatch, list[str]] | None:
        draft = await self.state.get_draft(token, telegram_user_id)
        if draft is None or draft.status not in {
            DraftStatus.PENDING,
            DraftStatus.DUPLICATE_WARNING,
        }:
            return None
        if not 0 <= item_index < len(draft.items):
            return None
        expense, income = await self.state.get_categories(telegram_user_id)
        categories = income if draft.items[item_index].type == TransactionType.INCOME else expense
        return draft, categories

    async def change_category(
        self,
        token: str,
        telegram_user_id: int,
        item_index: int,
        category: str,
    ) -> DraftBatch | None:
        selected = await self.categories_for_item(token, telegram_user_id, item_index)
        if selected is None:
            return None
        draft, categories = selected
        match = next(
            (value for value in categories if value.casefold() == category.casefold()),
            None,
        )
        if match is None:
            raise ValueError("Такої категорії немає")
        items = list(draft.items)
        items[item_index] = items[item_index].model_copy(update={"category": match})
        updated = draft.model_copy(update={"items": items, "status": DraftStatus.PENDING})
        await self.state.save_draft(updated)
        return updated

    def _transactions_from_draft(self, draft: DraftBatch) -> list[Transaction]:
        local_created = draft.created_at.astimezone(self.timezone)
        transactions: list[Transaction] = []
        for index, item in enumerate(draft.items):
            if item.amount is None:
                raise ValueError("Draft still has an unknown amount")
            occurred_date = item.date or local_created.date()
            occurred_at = datetime.combine(
                occurred_date,
                time(
                    local_created.hour,
                    local_created.minute,
                    local_created.second,
                    tzinfo=self.timezone,
                ),
            )
            transactions.append(
                Transaction(
                    id=str(uuid.uuid5(uuid.NAMESPACE_URL, f"finance-bot:{draft.token}:{index}")),
                    telegram_user_id=draft.telegram_user_id,
                    occurred_at=occurred_at,
                    type=item.type,
                    amount=item.amount,
                    currency=item.currency,
                    category=item.category,
                    description=item.description,
                    source=draft.source,
                    created_at=draft.created_at,
                )
            )
        return transactions

    async def confirm(
        self,
        token: str,
        telegram_user_id: int,
        *,
        force_duplicate: bool = False,
    ) -> ConfirmOutcome:
        async with self._user_locks[telegram_user_id]:
            draft = await self.state.get_draft(token, telegram_user_id)
            if draft is None:
                return ConfirmOutcome(ConfirmKind.UNAVAILABLE)
            if any(item.amount is None for item in draft.items):
                return ConfirmOutcome(ConfirmKind.NEEDS_AMOUNT, draft=draft)

            allowed = {DraftStatus.DUPLICATE_WARNING} if force_duplicate else {DraftStatus.PENDING}
            claimed = await self.state.transition_draft(
                token,
                telegram_user_id,
                allowed,
                DraftStatus.COMMITTING,
            )
            if not claimed:
                return ConfirmOutcome(ConfirmKind.UNAVAILABLE, draft=draft)

            transactions = self._transactions_from_draft(draft)
            sheet_id = await self.require_sheet_id(telegram_user_id)
            previous_status = (
                DraftStatus.DUPLICATE_WARNING if force_duplicate else DraftStatus.PENDING
            )
            try:
                if not force_duplicate:
                    duplicates = await self.sheets.find_duplicates(
                        sheet_id,
                        telegram_user_id,
                        transactions,
                        self.settings.duplicate_window_seconds,
                    )
                    if duplicates:
                        await self.state.transition_draft(
                            token,
                            telegram_user_id,
                            {DraftStatus.COMMITTING},
                            DraftStatus.DUPLICATE_WARNING,
                        )
                        warning_draft = draft.model_copy(
                            update={"status": DraftStatus.DUPLICATE_WARNING}
                        )
                        return ConfirmOutcome(
                            ConfirmKind.DUPLICATE,
                            draft=warning_draft,
                            transactions=transactions,
                            duplicates=duplicates,
                        )

                await self.sheets.append_transactions(sheet_id, transactions)
            except BaseException:
                await self.state.transition_draft(
                    token,
                    telegram_user_id,
                    {DraftStatus.COMMITTING},
                    previous_status,
                )
                raise

            await self.state.transition_draft(
                token,
                telegram_user_id,
                {DraftStatus.COMMITTING},
                DraftStatus.COMMITTED,
            )
            committed = draft.model_copy(update={"status": DraftStatus.COMMITTED})
            return ConfirmOutcome(
                ConfirmKind.SAVED,
                draft=committed,
                transactions=transactions,
            )

    async def cancel_draft(self, token: str, telegram_user_id: int) -> bool:
        return await self.state.transition_draft(
            token,
            telegram_user_id,
            {
                DraftStatus.PENDING,
                DraftStatus.NEEDS_AMOUNT,
                DraftStatus.DUPLICATE_WARNING,
            },
            DraftStatus.CANCELLED,
        )

    async def report(self, telegram_user_id: int, period: ReportPeriod) -> ReportSummary:
        sheet_id = await self.require_sheet_id(telegram_user_id)
        now = datetime.now(self.timezone)
        start, end = report_bounds(now, period)
        result = await self.sheets.list_between(sheet_id, start, end)
        return build_report(
            [
                transaction
                for transaction in result.transactions
                if transaction.telegram_user_id == telegram_user_id
            ],
            period=period,
            start=start,
            end=end,
            malformed_rows=result.malformed_rows,
        )

    async def undo(self, telegram_user_id: int) -> Transaction | None:
        sheet_id = await self.require_sheet_id(telegram_user_id)
        async with self._user_locks[telegram_user_id]:
            return await self.sheets.undo_last(sheet_id, telegram_user_id)

    async def add_category(self, telegram_user_id: int, kind: str, name: str) -> None:
        category_type = _category_kind(kind)
        value = _validate_category_name(name)
        expense, income = await self.state.get_categories(telegram_user_id)
        target = income if category_type == TransactionType.INCOME else expense
        if any(item.casefold() == value.casefold() for item in target):
            raise ValueError("Така категорія вже існує")
        target.append(value)
        await self.state.set_categories(telegram_user_id, expense, income)

    async def rename_category(
        self,
        telegram_user_id: int,
        kind: str,
        index: int,
        new_name: str,
    ) -> None:
        category_type = _category_kind(kind)
        value = _validate_category_name(new_name)
        expense, income = await self.state.get_categories(telegram_user_id)
        target = income if category_type == TransactionType.INCOME else expense
        if not 0 <= index < len(target):
            raise ValueError("Категорія вже недоступна")
        if target[index].casefold() == "інше":
            raise ValueError("Системну категорію «Інше» не можна перейменувати")
        if any(i != index and item.casefold() == value.casefold() for i, item in enumerate(target)):
            raise ValueError("Така категорія вже існує")
        target[index] = value
        await self.state.set_categories(telegram_user_id, expense, income)

    async def remove_category(self, telegram_user_id: int, kind: str, index: int) -> str:
        category_type = _category_kind(kind)
        expense, income = await self.state.get_categories(telegram_user_id)
        target = income if category_type == TransactionType.INCOME else expense
        if not 0 <= index < len(target):
            raise ValueError("Категорія вже недоступна")
        if target[index].casefold() == "інше":
            raise ValueError("Системну категорію «Інше» не можна видалити")
        removed = target.pop(index)
        await self.state.set_categories(telegram_user_id, expense, income)
        return removed
