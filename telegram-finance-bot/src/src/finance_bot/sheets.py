from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, TypeVar

from .domain import Currency, SourceType, Transaction, TransactionType

SHEET_HEADERS: tuple[str, ...] = (
    "Дата",
    "Час",
    "Тип",
    "Сума",
    "Валюта",
    "Категорія",
    "Опис",
    "Джерело",
    "_id",
    "_created_at",
    "_telegram_user_id",
)

_TYPE_TO_SHEET = {"expense": "витрата", "income": "дохід"}
_TYPE_FROM_SHEET = {
    "expense": "expense",
    "income": "income",
    "витрата": "expense",
    "витрати": "expense",
    "дохід": "income",
    "доходи": "income",
}
_SOURCE_TO_SHEET = {"text": "текст", "photo": "фото"}
_SOURCE_FROM_SHEET = {
    "text": "text",
    "photo": "photo",
    "текст": "text",
    "фото": "photo",
}

_T = TypeVar("_T")


class SheetSchemaConflictError(RuntimeError):
    """Raised when an existing worksheet does not have the bot's exact schema."""

    def __init__(self, actual_headers: Sequence[str]) -> None:
        self.actual_headers = tuple(actual_headers)
        self.expected_headers = SHEET_HEADERS
        super().__init__(
            "Google Sheet schema conflict: expected "
            f"{list(self.expected_headers)!r}, got {list(self.actual_headers)!r}"
        )


# A concise alias is convenient at call sites and keeps compatibility with early clients.
SheetSchemaConflict = SheetSchemaConflictError


@dataclass(frozen=True, slots=True)
class QueryResult:
    transactions: list[Transaction]
    malformed_rows: int = 0


def load_service_account_info(credentials: Mapping[str, Any] | str | Path) -> dict[str, Any]:
    """Return a service-account mapping from a dict, JSON string, or JSON file path."""

    if isinstance(credentials, Mapping):
        result = dict(credentials)
    else:
        raw = str(credentials).strip()
        if not raw:
            raise ValueError("Google service-account credentials cannot be empty")
        if raw.startswith("{"):
            parsed = json.loads(raw)
        else:
            path = Path(raw).expanduser()
            if not path.is_file():
                raise ValueError(
                    "Google credentials must be a service-account dict, JSON string, "
                    "or an existing JSON file"
                )
            parsed = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(parsed, dict):
            raise ValueError("Google service-account JSON must contain an object")
        result = parsed
    if result.get("type") not in (None, "service_account"):
        raise ValueError("Google credentials are not service-account credentials")
    if not result.get("client_email"):
        raise ValueError("Google service-account credentials do not contain client_email")
    return result


def create_gspread_client(credentials: Mapping[str, Any] | str | Path) -> Any:
    """Construct an authenticated gspread client without making a Sheets API request."""

    import gspread

    return gspread.service_account_from_dict(load_service_account_info(credentials))


def _enum_value(value: Any) -> str:
    raw = getattr(value, "value", value)
    return str(raw)


def _aware(value: datetime, *, field: str) -> datetime:
    if value.tzinfo is None:
        raise ValueError(f"{field} must be timezone-aware")
    return value


def serialize_transaction(transaction: Transaction) -> list[str]:
    """Serialize one transaction to the canonical RAW Google Sheets row."""

    occurred_at = _aware(transaction.occurred_at, field="occurred_at")
    created_at = _aware(transaction.created_at, field="created_at")
    type_value = _enum_value(transaction.type)
    source_value = _enum_value(transaction.source)
    if type_value not in _TYPE_TO_SHEET:
        raise ValueError(f"unsupported transaction type: {type_value!r}")
    if source_value not in _SOURCE_TO_SHEET:
        raise ValueError(f"unsupported source type: {source_value!r}")
    amount = Decimal(transaction.amount)
    if not amount.is_finite() or amount <= 0:
        raise ValueError("transaction amount must be a finite positive number")

    return [
        occurred_at.date().isoformat(),
        occurred_at.timetz().isoformat(timespec="seconds"),
        _TYPE_TO_SHEET[type_value],
        format(amount, "f"),
        _enum_value(transaction.currency).upper(),
        transaction.category,
        transaction.description,
        _SOURCE_TO_SHEET[source_value],
        str(transaction.id),
        created_at.isoformat(),
        str(transaction.telegram_user_id),
    ]


def _cell(row: Sequence[Any], index: int) -> str:
    if index >= len(row) or row[index] is None:
        return ""
    return str(row[index]).strip()


def _parse_datetime(date_text: str, time_text: str) -> datetime:
    if not date_text or not time_text:
        raise ValueError("date and time cells are required")
    try:
        value = datetime.fromisoformat(f"{date_text}T{time_text}")
    except ValueError as error:
        raise ValueError("invalid date or time cell") from error
    # Old/manual sheets may contain local-looking values. UTC is deterministic and keeps
    # the domain invariant; bot-written rows always include their original UTC offset.
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value


def _parse_created_at(value: str) -> datetime:
    try:
        result = datetime.fromisoformat(value)
    except ValueError as error:
        raise ValueError("invalid _created_at cell") from error
    if result.tzinfo is None:
        result = result.replace(tzinfo=UTC)
    return result


def deserialize_transaction(row: Sequence[Any]) -> Transaction:
    """Parse a canonical Sheets row, raising ``ValueError`` for malformed data."""

    if len(row) < len(SHEET_HEADERS):
        raise ValueError(f"row has {len(row)} cells; expected {len(SHEET_HEADERS)}")
    type_text = _cell(row, 2).casefold()
    source_text = _cell(row, 7).casefold()
    try:
        transaction_type = TransactionType(_TYPE_FROM_SHEET[type_text])
    except (KeyError, ValueError) as error:
        raise ValueError(f"invalid transaction type: {_cell(row, 2)!r}") from error
    try:
        source = SourceType(_SOURCE_FROM_SHEET[source_text])
    except (KeyError, ValueError) as error:
        raise ValueError(f"invalid transaction source: {_cell(row, 7)!r}") from error
    try:
        amount = Decimal(_cell(row, 3).replace("\u00a0", "").replace(",", "."))
    except InvalidOperation as error:
        raise ValueError("invalid amount cell") from error
    if not amount.is_finite() or amount <= 0:
        raise ValueError("amount must be a finite positive number")
    try:
        currency = Currency(_cell(row, 4).upper())
    except ValueError as error:
        raise ValueError(f"invalid currency: {_cell(row, 4)!r}") from error

    transaction_id = _cell(row, 8)
    category = _cell(row, 5)
    description = _cell(row, 6)
    if not transaction_id or not category or not description:
        raise ValueError("_id, category and description cells are required")
    try:
        telegram_user_id = int(_cell(row, 10))
    except ValueError as error:
        raise ValueError("invalid _telegram_user_id cell") from error

    return Transaction(
        id=transaction_id,
        telegram_user_id=telegram_user_id,
        occurred_at=_parse_datetime(_cell(row, 0), _cell(row, 1)),
        type=transaction_type,
        amount=amount,
        currency=currency,
        category=category,
        description=description,
        source=source,
        created_at=_parse_created_at(_cell(row, 9)),
    )


def _trim_trailing_empty(values: Sequence[Any]) -> list[str]:
    result = ["" if value is None else str(value).strip() for value in values]
    while result and not result[-1]:
        result.pop()
    return result


def _is_retryable(error: BaseException) -> bool:
    if isinstance(error, (SheetSchemaConflictError, ValueError, TypeError, KeyError)):
        return False
    response = getattr(error, "response", None)
    status = getattr(response, "status_code", None)
    if status is not None:
        return status == 429 or status >= 500
    # Transport failures from requests/urllib3 are transient.  Looking at the module keeps
    # this module importable without making requests a direct project dependency.
    module = type(error).__module__
    if module.startswith(("requests", "urllib3", "httpx", "google.auth.transport")):
        return True
    return type(error).__name__ in {
        "TimeoutError",
        "ConnectionError",
        "ReadTimeout",
        "ConnectTimeout",
        "TransportError",
    }


class GoogleSheetsRepository:
    """Async, retrying gspread repository with idempotent transaction appends."""

    def __init__(
        self,
        credentials: Mapping[str, Any] | str | Path | None = None,
        *,
        client: Any | None = None,
        worksheet_title: str = "Операції",
        max_concurrency: int = 4,
        max_retries: int = 3,
        retry_base_seconds: float = 0.35,
    ) -> None:
        if client is None and credentials is None:
            raise ValueError("credentials or an authenticated gspread client is required")
        if max_concurrency < 1:
            raise ValueError("max_concurrency must be at least 1")
        if max_retries < 0:
            raise ValueError("max_retries cannot be negative")
        self._credentials = (
            load_service_account_info(credentials) if credentials is not None else None
        )
        self._client = client or create_gspread_client(self._credentials or {})
        self.worksheet_title = worksheet_title
        self.max_retries = max_retries
        self.retry_base_seconds = retry_base_seconds
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._sheet_locks: dict[str, asyncio.Lock] = {}
        self._sheet_locks_guard = asyncio.Lock()

    @classmethod
    def from_credentials(
        cls,
        credentials: Mapping[str, Any] | str | Path,
        **kwargs: Any,
    ) -> GoogleSheetsRepository:
        return cls(credentials, **kwargs)

    @property
    def service_account_email(self) -> str | None:
        if self._credentials:
            value = self._credentials.get("client_email")
            return str(value) if value else None
        auth = getattr(self._client, "auth", None)
        for attribute in ("service_account_email", "client_email"):
            value = getattr(auth, attribute, None)
            if value:
                return str(value)
        return None

    async def _sheet_lock(self, sheet_id: str) -> asyncio.Lock:
        async with self._sheet_locks_guard:
            return self._sheet_locks.setdefault(sheet_id, asyncio.Lock())

    async def _execute(self, function: Callable[[], _T]) -> _T:
        async with self._semaphore:
            for attempt in range(self.max_retries + 1):
                try:
                    return await asyncio.to_thread(function)
                except BaseException as error:
                    if attempt >= self.max_retries or not _is_retryable(error):
                        raise
                    await asyncio.sleep(self.retry_base_seconds * (2**attempt))
        raise AssertionError("unreachable")

    def _worksheet_sync(self, sheet_id: str, *, create: bool) -> Any:
        spreadsheet = self._client.open_by_key(sheet_id)
        try:
            return spreadsheet.worksheet(self.worksheet_title)
        except BaseException as error:
            if not create or type(error).__name__ != "WorksheetNotFound":
                raise
            return spreadsheet.add_worksheet(
                title=self.worksheet_title,
                rows=1000,
                cols=max(20, len(SHEET_HEADERS)),
            )

    @staticmethod
    def _write_header_sync(worksheet: Any) -> None:
        worksheet.update(
            values=[list(SHEET_HEADERS)],
            range_name=f"A1:{_column_letter(len(SHEET_HEADERS))}1",
            value_input_option="RAW",
        )
        # Metadata columns are for idempotency and undo, not day-to-day viewing.  Hiding
        # is best-effort because lightweight gspread test doubles often omit this method.
        hide_columns = getattr(worksheet, "hide_columns", None)
        if callable(hide_columns):
            try:
                hide_columns(8, 11)
            except (AttributeError, NotImplementedError):
                pass

    def _ensure_schema_sync(self, sheet_id: str) -> Any:
        worksheet = self._worksheet_sync(sheet_id, create=True)
        actual = _trim_trailing_empty(worksheet.row_values(1))
        if not actual:
            self._write_header_sync(worksheet)
        elif tuple(actual) != SHEET_HEADERS:
            raise SheetSchemaConflictError(actual)
        return worksheet

    async def ensure_schema(self, sheet_id: str) -> None:
        lock = await self._sheet_lock(sheet_id)
        async with lock:
            await self._execute(lambda: self._ensure_schema_sync(sheet_id))

    async def append_transactions(self, sheet_id: str, transactions: list[Transaction]) -> int:
        if not transactions:
            return 0
        # Preserve caller order while rejecting duplicate IDs inside the same request.
        unique: list[Transaction] = []
        target_ids: set[str] = set()
        for transaction in transactions:
            transaction_id = str(transaction.id)
            if transaction_id not in target_ids:
                unique.append(transaction)
                target_ids.add(transaction_id)

        baseline_ids: set[str] | None = None

        def append_sync() -> int:
            nonlocal baseline_ids
            worksheet = self._ensure_schema_sync(sheet_id)
            existing_ids = {value.strip() for value in worksheet.col_values(9)[1:] if value.strip()}
            if baseline_ids is None:
                baseline_ids = set(existing_ids)
            missing = [item for item in unique if str(item.id) not in existing_ids]
            if missing:
                worksheet.append_rows(
                    [serialize_transaction(item) for item in missing],
                    value_input_option="RAW",
                    insert_data_option="INSERT_ROWS",
                    table_range="A:K",
                )
            return len(target_ids - baseline_ids)

        lock = await self._sheet_lock(sheet_id)
        async with lock:
            return await self._execute(append_sync)

    async def list_between(
        self,
        sheet_id: str,
        start: datetime,
        end: datetime,
    ) -> QueryResult:
        _aware(start, field="start")
        _aware(end, field="end")
        if end < start:
            raise ValueError("end must not be earlier than start")

        def list_sync() -> QueryResult:
            worksheet = self._ensure_schema_sync(sheet_id)
            transactions: list[Transaction] = []
            malformed_rows = 0
            for row in worksheet.get_all_values()[1:]:
                if not any(str(cell).strip() for cell in row):
                    continue
                try:
                    transaction = deserialize_transaction(row)
                except (ValueError, TypeError):
                    malformed_rows += 1
                    continue
                if start <= transaction.occurred_at <= end:
                    transactions.append(transaction)
            transactions.sort(key=lambda item: (item.occurred_at, item.created_at, item.id))
            return QueryResult(transactions=transactions, malformed_rows=malformed_rows)

        lock = await self._sheet_lock(sheet_id)
        async with lock:
            return await self._execute(list_sync)

    async def find_duplicates(
        self,
        sheet_id: str,
        telegram_user_id: int,
        transactions: list[Transaction],
        within_seconds: int = 15,
    ) -> list[Transaction]:
        if within_seconds < 0:
            raise ValueError("within_seconds cannot be negative")
        if not transactions:
            return []

        def duplicates_sync() -> list[Transaction]:
            worksheet = self._ensure_schema_sync(sheet_id)
            existing: list[Transaction] = []
            for row in worksheet.get_all_values()[1:]:
                if not any(str(cell).strip() for cell in row):
                    continue
                try:
                    item = deserialize_transaction(row)
                except (ValueError, TypeError):
                    continue
                if item.telegram_user_id == telegram_user_id:
                    existing.append(item)

            result: list[Transaction] = []
            seen: set[str] = set()
            for old in existing:
                for new in transactions:
                    if str(old.id) == str(new.id):
                        continue
                    same_payload = (
                        old.amount == new.amount
                        and old.currency == new.currency
                        and old.type == new.type
                        and old.description.strip().casefold() == new.description.strip().casefold()
                    )
                    seconds = abs((old.created_at - new.created_at).total_seconds())
                    if same_payload and seconds <= within_seconds and str(old.id) not in seen:
                        result.append(old)
                        seen.add(str(old.id))
                        break
            result.sort(key=lambda item: item.created_at, reverse=True)
            return result

        lock = await self._sheet_lock(sheet_id)
        async with lock:
            return await self._execute(duplicates_sync)

    async def undo_last(self, sheet_id: str, telegram_user_id: int) -> Transaction | None:
        def undo_sync() -> Transaction | None:
            worksheet = self._ensure_schema_sync(sheet_id)
            candidates: list[tuple[datetime, int, Transaction]] = []
            for row_index, row in enumerate(worksheet.get_all_values()[1:], start=2):
                if not any(str(cell).strip() for cell in row):
                    continue
                try:
                    transaction = deserialize_transaction(row)
                except (ValueError, TypeError):
                    continue
                if transaction.telegram_user_id == telegram_user_id:
                    candidates.append((transaction.created_at, row_index, transaction))
            if not candidates:
                return None
            _, row_index, transaction = max(candidates, key=lambda item: (item[0], item[1]))
            worksheet.delete_rows(row_index)
            return transaction

        lock = await self._sheet_lock(sheet_id)
        async with lock:
            return await self._execute(undo_sync)


def _column_letter(number: int) -> str:
    if number < 1:
        raise ValueError("column number must be positive")
    letters = ""
    while number:
        number, remainder = divmod(number - 1, 26)
        letters = chr(ord("A") + remainder) + letters
    return letters


__all__ = [
    "SHEET_HEADERS",
    "GoogleSheetsRepository",
    "QueryResult",
    "SheetSchemaConflict",
    "SheetSchemaConflictError",
    "create_gspread_client",
    "deserialize_transaction",
    "load_service_account_info",
    "serialize_transaction",
]
