from datetime import UTC, datetime
from decimal import Decimal

from finance_bot.domain import Currency, SourceType, Transaction, TransactionType
from finance_bot.table_image import _fmt_totals, generate_table_image_from_bytes


def test_empty_table_is_rendered_as_png() -> None:
    start = datetime(2026, 7, 6, tzinfo=UTC)
    end = datetime(2026, 7, 12, tzinfo=UTC)

    image = generate_table_image_from_bytes([], period="week", start=start, end=end)

    assert image.startswith(b"\x89PNG\r\n\x1a\n")
    assert len(image) > 1_000


def test_totals_keep_currencies_separate() -> None:
    totals = {"UAH": Decimal("1250"), "USD": Decimal("20.50")}

    assert _fmt_totals(totals, ["UAH", "USD"]) == "1 250 грн / 20.50 $"


def test_table_with_transaction_is_rendered_as_png() -> None:
    occurred_at = datetime(2026, 7, 10, 12, 30, tzinfo=UTC)
    transaction = Transaction(
        id="transaction-1",
        telegram_user_id=1,
        occurred_at=occurred_at,
        type=TransactionType.EXPENSE,
        amount=Decimal("125.50"),
        currency=Currency.UAH,
        category="Їжа",
        description="Обід",
        source=SourceType.TEXT,
        created_at=occurred_at,
    )

    image = generate_table_image_from_bytes(
        [transaction],
        period="week",
        start=datetime(2026, 7, 6, tzinfo=UTC),
        end=datetime(2026, 7, 12, tzinfo=UTC),
    )

    assert image.startswith(b"\x89PNG\r\n\x1a\n")
    assert len(image) > 1_000
