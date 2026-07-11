"""Generate a styled PNG table image from transactions for Telegram."""

from __future__ import annotations

import io
from datetime import datetime
from decimal import Decimal
from typing import Literal

from .domain import Transaction, TransactionType

PeriodLabel = Literal["week", "month"]

_CURRENCY_SYMBOL: dict[str, str] = {"UAH": "грн", "USD": "$", "EUR": "€"}


def _fmt_amount(amount: Decimal, currency: str) -> str:
    n = amount.quantize(Decimal("0.01"))
    text = f"{n:,.2f}".replace(",", " ") if n != n.to_integral() else f"{n:,.0f}".replace(",", " ")
    return f"{text} {_CURRENCY_SYMBOL.get(currency, currency)}"


def generate_table_image(
    transactions: list[Transaction],
    *,
    period: PeriodLabel = "week",
    start: datetime,
    end: datetime,
) -> io.BytesIO:
    """Return a BytesIO PNG buffer with a styled transactions table."""

    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.font_manager as fm

    # --- Font setup (works on Linux/Windows/macOS) ---
    _register_fonts(fm)

    # --- Data preparation ---
    period_text = "Тиждень" if period == "week" else "Місяць"
    date_range = f"{start.strftime('%d.%m.%Y')} — {end.strftime('%d.%m.%Y')}"

    # Sort by occurred_at descending (newest first)
    sorted_tx = sorted(transactions, key=lambda t: (t.occurred_at, t.created_at), reverse=True)

    if not sorted_tx:
        return _empty_table_image(period_text, date_range)

    # Truncate to 30 rows for readability
    display_tx = sorted_tx[:30]
    truncated = len(sorted_tx) - 30

    # --- Build figure ---
    n_rows = len(display_tx) + 1  # +1 for header
    fig_width = 11
    row_height = 0.52
    fig_height = 1.8 + n_rows * row_height  # header space + data rows

    fig, ax = plt.subplots(figsize=(fig_width, fig_height))
    ax.set_axis_off()

    # --- Colors ---
    BG_COLOR = "#1e1e2e"
    HEADER_BG = "#45475a"
    HEADER_TEXT = "#cdd6f4"
    INCOME_BG = "#1e3a2f"
    INCOME_TEXT = "#a6e3a1"
    EXPENSE_BG = "#3a1e1e"
    EXPENSE_TEXT = "#f38ba8"
    ROW_ALT = "#262637"
    BORDER_COLOR = "#585b70"
    SUMMARY_BG = "#313244"
    TITLE_COLOR = "#cdd6f4"
    SUBTITLE_COLOR = "#a6adc8"

    fig.set_facecolor(BG_COLOR)

    # --- Column widths (total = 1.0) ---
    col_widths = [0.12, 0.08, 0.08, 0.17, 0.08, 0.22, 0.25]
    col_x = [0.0]
    for w in col_widths[:-1]:
        col_x.append(col_x[-1] + w)

    headers = ["Дата", "Час", "Тип", "Сума", "Валюта", "Категорія", "Опис"]
    font_family = "DejaVu Sans"

    # --- Title ---
    y_top = 1.0 - 0.02
    ax.text(
        0.5, y_top,
        f"Фінансовий звіт — {period_text}",
        ha="center", va="top",
        fontsize=16, fontweight="bold",
        color=TITLE_COLOR,
        fontfamily=font_family,
        transform=ax.transAxes,
    )
    ax.text(
        0.5, y_top - 0.065,
        date_range,
        ha="center", va="top",
        fontsize=10,
        color=SUBTITLE_COLOR,
        fontfamily=font_family,
        transform=ax.transAxes,
    )

    # --- Table area ---
    table_top = y_top - 0.13
    cell_h = row_height / fig_height

    # --- Draw header row ---
    header_y = table_top
    for i, (header, x) in enumerate(zip(headers, col_x)):
        w = col_widths[i]
        rect = plt.Rectangle(
            (x, header_y - cell_h), w, cell_h,
            transform=ax.transAxes,
            facecolor=HEADER_BG, edgecolor=BORDER_COLOR, linewidth=0.5,
            clip_on=False,
        )
        ax.add_patch(rect)
        ax.text(
            x + w / 2, header_y - cell_h / 2,
            header,
            ha="center", va="center",
            fontsize=9, fontweight="bold",
            color=HEADER_TEXT,
            fontfamily=font_family,
            transform=ax.transAxes,
        )

    # --- Draw data rows ---
    total_income = Decimal("0")
    total_expense = Decimal("0")

    for row_idx, tx in enumerate(display_tx):
        y = header_y - cell_h * (row_idx + 1)
        is_income = tx.type == TransactionType.INCOME
        row_bg = INCOME_BG if is_income else EXPENSE_BG
        if row_idx % 2 == 1:
            # Slightly darken alternating rows
            row_bg = ROW_ALT if not is_income else "#1a3530"

        if is_income:
            total_income += tx.amount
        else:
            total_expense += tx.amount

        values = [
            tx.occurred_at.astimezone(None).strftime("%d.%m.%Y"),
            tx.occurred_at.astimezone(None).strftime("%H:%M"),
            "Дохід" if is_income else "Витрата",
            _fmt_amount(tx.amount, tx.currency.value),
            tx.currency.value,
            tx.category[:20] + "…" if len(tx.category) > 20 else tx.category,
            tx.description[:28] + "…" if len(tx.description) > 28 else tx.description,
        ]

        text_color = INCOME_TEXT if is_income else EXPENSE_TEXT

        for col_idx, (val, x) in enumerate(zip(values, col_x)):
            w = col_widths[col_idx]
            rect = plt.Rectangle(
                (x, y - cell_h), w, cell_h,
                transform=ax.transAxes,
                facecolor=row_bg, edgecolor=BORDER_COLOR, linewidth=0.3,
                clip_on=False,
            )
            ax.add_patch(rect)
            ha = "left" if col_idx >= 5 else "center"
            x_pos = x + 0.01 if col_idx >= 5 else x + w / 2
            ax.text(
                x_pos, y - cell_h / 2,
                val,
                ha=ha, va="center",
                fontsize=8.5,
                color=text_color,
                fontfamily=font_family,
                transform=ax.transAxes,
            )

    # --- Summary row ---
    summary_y = header_y - cell_h * (len(display_tx) + 1)
    balance = total_income - total_expense
    balance_sign = "+" if balance >= 0 else ""

    # Income summary
    for col_idx, (val, x) in enumerate([
        ("", ""),
        ("", ""),
        ("", ""),
        (f"+{_fmt_amount(total_income, 'UAH')}" if not any(t.currency.value != "UAH" for t in display_tx) else f"+{total_income:,.2f}", ""),
        ("", ""),
        ("РАЗОМ ДОХІД", ""),
        ("", ""),
    ]):
        pass  # We'll draw a simpler summary

    # Draw two summary cells: income and expense
    sum_cell_h = cell_h * 0.9

    # Income cell
    income_text = f"Доходи: {_fmt_amount(total_income, 'UAH')}"
    expense_text = f"Витрати: {_fmt_amount(total_expense, 'UAH')}"
    balance_text = f"Баланс: {balance_sign}{_fmt_amount(abs(balance), 'UAH')}"
    balance_color = INCOME_TEXT if balance >= 0 else EXPENSE_TEXT

    summary_y_start = summary_y - 0.005
    for label, value, color in [
        (income_text, None, INCOME_TEXT),
        (expense_text, None, EXPENSE_TEXT),
        (balance_text, None, balance_color),
    ]:
        y_pos = summary_y_start - sum_cell_h * 0.33
        rect = plt.Rectangle(
            (0, summary_y_start - sum_cell_h), 1.0, sum_cell_h,
            transform=ax.transAxes,
            facecolor=SUMMARY_BG, edgecolor=BORDER_COLOR, linewidth=0.5,
            clip_on=False,
        )
        ax.add_patch(rect)
        ax.text(
            0.03, y_pos - 0.002,
            label,
            ha="left", va="center",
            fontsize=10, fontweight="bold",
            color=color,
            fontfamily=font_family,
            transform=ax.transAxes,
        )
        summary_y_start -= sum_cell_h * 0.33
        break  # Draw only one background rect

    # Draw all three summary texts
    sy = header_y - cell_h * (len(display_tx) + 0.5)
    rect = plt.Rectangle(
        (0, sy - cell_h * 1.1), 1.0, cell_h * 1.1,
        transform=ax.transAxes,
        facecolor=SUMMARY_BG, edgecolor=BORDER_COLOR, linewidth=0.5,
        clip_on=False,
    )
    ax.add_patch(rect)

    ax.text(
        0.03, sy - cell_h * 0.25,
        income_text,
        ha="left", va="center",
        fontsize=10, fontweight="bold",
        color=INCOME_TEXT,
        fontfamily=font_family,
        transform=ax.transAxes,
    )
    ax.text(
        0.40, sy - cell_h * 0.25,
        expense_text,
        ha="left", va="center",
        fontsize=10, fontweight="bold",
        color=EXPENSE_TEXT,
        fontfamily=font_family,
        transform=ax.transAxes,
    )
    ax.text(
        0.78, sy - cell_h * 0.25,
        balance_text,
        ha="left", va="center",
        fontsize=10, fontweight="bold",
        color=balance_color,
        fontfamily=font_family,
        transform=ax.transAxes,
    )

    if truncated > 0:
        ax.text(
            0.5, sy - cell_h * 0.75,
            f"... та ще {truncated} операцій не показано",
            ha="center", va="center",
            fontsize=8,
            color=SUBTITLE_COLOR,
            fontfamily=font_family,
            transform=ax.transAxes,
            style="italic",
        )

    # --- Adjust layout and save ---
    margin = 0.02
    ax.set_xlim(margin, 1 - margin)
    ax.set_ylim(0, 1)

    buf = io.BytesIO()
    fig.savefig(
        buf,
        format="png",
        dpi=150,
        bbox_inches="tight",
        facecolor=fig.get_facecolor(),
        pad_inches=0.15,
    )
    plt.close(fig)
    buf.seek(0)
    return buf


def _empty_table_image(period_text: str, date_range: str) -> io.BytesIO:
    """Generate a placeholder image when there are no transactions."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.font_manager as fm

    _register_fonts(fm)

    fig, ax = plt.subplots(figsize=(6, 2.5))
    ax.set_axis_off()
    fig.set_facecolor("#1e1e2e")

    ax.text(
        0.5, 0.65,
        f"Фінансовий звіт — {period_text}",
        ha="center", va="center",
        fontsize=14, fontweight="bold",
        color="#cdd6f4",
        fontfamily="DejaVu Sans",
        transform=ax.transAxes,
    )
    ax.text(
        0.5, 0.45,
        date_range,
        ha="center", va="center",
        fontsize=10,
        color="#a6adc8",
        fontfamily="DejaVu Sans",
        transform=ax.transAxes,
    )
    ax.text(
        0.5, 0.2,
        "Поки що немає операцій за цей період.",
        ha="center", va="center",
        fontsize=11,
        color="#6c7086",
        fontfamily="DejaVu Sans",
        transform=ax.transAxes,
    )

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=150, bbox_inches="tight", facecolor=fig.get_facecolor(), pad_inches=0.2)
    plt.close(fig)
    buf.seek(0)
    return buf


def _register_fonts(fm: object) -> None:
    """Register fonts with Cyrillic + CJK support."""
    import os

    font_paths = [
        # Linux CJK
        "/usr/share/fonts/truetype/chinese/NotoSansSC-Regular.ttf",
        "/usr/share/fonts/truetype/noto-serif-sc/NotoSerifSC-Regular.ttf",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        # Linux Cyrillic/Latin
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        "/usr/share/fonts/truetype/freefont/FreeSans.ttf",
        # Windows
        "C:\\Windows\\Fonts\\dejavusans.ttf",
        "C:\\Windows\\Fonts\\arial.ttf",
        "C:\\Windows\\Fonts\\segoeui.ttf",
        "C:\\Windows\\Fonts\\msyh.ttc",
        # macOS
        "/System/Library/Fonts/PingFang.ttc",
        "/System/Library/Fonts/Helvetica.ttc",
    ]
    for path in font_paths:
        if os.path.isfile(path):
            try:
                fm.fontManager.addfont(path)
            except Exception:
                pass
    # DejaVu Sans has great Cyrillic support, put it first.
    # Noto Sans SC / Microsoft YaHei for CJK fallback.
    plt_rc = {
        "font.sans-serif": [
            "DejaVu Sans",
            "Liberation Sans",
            "FreeSans",
            "Noto Sans SC",
            "Microsoft YaHei",
            "PingFang SC",
            "Arial",
            "Segoe UI",
        ],
        "axes.unicode_minus": False,
    }
    import matplotlib.pyplot as plt
    plt.rcParams.update(plt_rc)


def generate_table_image_from_bytes(
    transactions: list[Transaction],
    *,
    period: PeriodLabel = "week",
    start: datetime,
    end: datetime,
) -> bytes:
    """Convenience wrapper that returns raw PNG bytes."""
    buf = generate_table_image(transactions, period=period, start=start, end=end)
    return buf.getvalue()