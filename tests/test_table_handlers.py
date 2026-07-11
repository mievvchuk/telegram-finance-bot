from aiogram.types import BufferedInputFile

from finance_bot.handlers import _table_keyboard, _table_photo


def test_table_photo_wraps_png_as_telegram_upload() -> None:
    payload = b"\x89PNG\r\n\x1a\nexample"

    photo = _table_photo(payload, "week")

    assert isinstance(photo, BufferedInputFile)
    assert photo.data == payload
    assert photo.filename == "finance-table-week.png"


def test_table_keyboard_contains_both_periods() -> None:
    keyboard = _table_keyboard("week")

    assert [button.callback_data for button in keyboard.inline_keyboard[0]] == [
        "table:week",
        "table:month",
    ]
