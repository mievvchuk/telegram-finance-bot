from __future__ import annotations

import re
import unicodedata
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

MAX_AMOUNT = Decimal("1000000000")

DEFAULT_EXPENSE_CATEGORIES: tuple[str, ...] = (
    "Продукти",
    "Кафе/ресторани",
    "Транспорт",
    "Комунальні послуги",
    "Здоров'я",
    "Одяг",
    "Розваги",
    "Підписки",
    "Інше",
)
DEFAULT_INCOME_CATEGORIES: tuple[str, ...] = (
    "Зарплата",
    "Підробіток",
    "Подарунок",
    "Інше",
)


class TransactionType(StrEnum):
    EXPENSE = "expense"
    INCOME = "income"


class Currency(StrEnum):
    UAH = "UAH"
    USD = "USD"
    EUR = "EUR"


class Confidence(StrEnum):
    HIGH = "high"
    LOW = "low"


class SourceType(StrEnum):
    TEXT = "text"
    PHOTO = "photo"


class ParseStatus(StrEnum):
    OK = "ok"
    NOT_FINANCIAL = "not_financial"
    UNREADABLE = "unreadable"


class DraftStatus(StrEnum):
    PENDING = "pending"
    NEEDS_AMOUNT = "needs_amount"
    COMMITTING = "committing"
    DUPLICATE_WARNING = "duplicate_warning"
    COMMITTED = "committed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


def normalize_description(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value)
    return " ".join(normalized.split()).strip()


class ParsedItem(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    type: TransactionType
    amount: Decimal | None
    currency: Currency = Currency.UAH
    category: str = Field(min_length=1, max_length=40)
    description: str = Field(min_length=1, max_length=120)
    date: date | None
    confidence: Confidence

    @field_validator("amount")
    @classmethod
    def validate_amount(cls, value: Decimal | None) -> Decimal | None:
        if value is None:
            return None
        if not value.is_finite() or value <= 0 or value > MAX_AMOUNT:
            raise ValueError("amount must be finite, positive and no greater than 1 billion")
        return value

    @field_validator("category", "description")
    @classmethod
    def normalize_text(cls, value: str) -> str:
        result = normalize_description(value)
        if not result:
            raise ValueError("text value cannot be blank")
        return result


class ParseEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: ParseStatus
    items: list[ParsedItem] = Field(default_factory=list, max_length=20)
    issue: str | None = Field(default=None, max_length=240)

    @model_validator(mode="after")
    def status_matches_items(self) -> Self:
        if self.status == ParseStatus.OK and not self.items:
            raise ValueError("status=ok requires at least one item")
        if self.status != ParseStatus.OK and self.items:
            raise ValueError("non-ok status cannot contain items")
        return self


def _aware(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value


class DraftBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    token: str = Field(min_length=6, max_length=32, pattern=r"^[A-Za-z0-9_-]+$")
    telegram_user_id: int = Field(gt=0)
    items: list[ParsedItem] = Field(min_length=1, max_length=20)
    source: SourceType
    status: DraftStatus
    original_text: str | None = Field(default=None, max_length=4096)
    created_at: datetime
    expires_at: datetime

    @field_validator("created_at", "expires_at")
    @classmethod
    def validate_aware(cls, value: datetime, info: object) -> datetime:
        field_name = getattr(info, "field_name", "datetime")
        return _aware(value, field_name)

    @model_validator(mode="after")
    def expiry_after_creation(self) -> Self:
        if self.expires_at <= self.created_at:
            raise ValueError("expires_at must be after created_at")
        return self


class Transaction(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    id: str = Field(min_length=1, max_length=64)
    telegram_user_id: int = Field(gt=0)
    occurred_at: datetime
    type: TransactionType
    amount: Decimal
    currency: Currency
    category: str = Field(min_length=1, max_length=40)
    description: str = Field(min_length=1, max_length=120)
    source: SourceType
    created_at: datetime

    @field_validator("amount")
    @classmethod
    def validate_amount(cls, value: Decimal) -> Decimal:
        checked = ParsedItem.validate_amount(value)
        assert checked is not None
        return checked

    @field_validator("occurred_at", "created_at")
    @classmethod
    def validate_aware(cls, value: datetime, info: object) -> datetime:
        field_name = getattr(info, "field_name", "datetime")
        return _aware(value, field_name)

    @field_validator("category", "description")
    @classmethod
    def normalize_text(cls, value: str) -> str:
        return ParsedItem.normalize_text(value)


_AMOUNT_PATTERN = re.compile(
    r"(?<![\w\-−])(?P<integer>\d{1,3}(?:[ \u00a0]\d{3})+|\d+)"
    r"(?P<fraction>[,.]\d{1,2})?"
)
_SHEET_URL_PATTERN = re.compile(
    r"https?://docs\.google\.com/spreadsheets/d/([A-Za-z0-9_-]{20,})",
    re.IGNORECASE,
)
_RAW_SHEET_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{20,}$")


def parse_manual_amount(text: str) -> Decimal | None:
    match = _AMOUNT_PATTERN.search(unicodedata.normalize("NFKC", text))
    if match is None:
        return None
    number = match.group("integer").replace(" ", "").replace("\u00a0", "")
    fraction = (match.group("fraction") or "").replace(",", ".")
    try:
        amount = Decimal(number + fraction)
    except InvalidOperation:
        return None
    if not amount.is_finite() or amount <= 0 or amount > MAX_AMOUNT:
        return None
    return amount


def extract_google_sheet_id(value: str) -> str:
    candidate = value.strip()
    url_match = _SHEET_URL_PATTERN.search(candidate)
    if url_match:
        return url_match.group(1)
    if _RAW_SHEET_ID_PATTERN.fullmatch(candidate):
        return candidate
    raise ValueError("invalid Google Sheets URL or spreadsheet ID")


# Compatibility aliases for integrations that use the generic names.
OperationType = TransactionType
Source = SourceType
