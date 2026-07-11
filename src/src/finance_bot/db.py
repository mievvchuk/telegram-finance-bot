from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .domain import DraftBatch, DraftStatus

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


def _status_value(status: DraftStatus | str) -> str:
    return status.value if isinstance(status, DraftStatus) else str(status)


def _utc_iso(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("datetime values stored in SQLite must be timezone-aware")
    return value.astimezone(UTC).isoformat()


def _normalize_categories(values: Iterable[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for raw_value in values:
        value = raw_value.strip()
        if not value:
            raise ValueError("category names cannot be empty")
        key = value.casefold()
        if key in seen:
            continue
        seen.add(key)
        result.append(value)
    return result


class SQLiteState:
    """Small durable state store for users, categories and pending drafts.

    A single connection is intentionally kept for the lifetime of the object.  Besides
    being inexpensive for a bot, this also makes ``:memory:`` databases useful in tests.
    Calls are serialized and moved to a worker thread so sqlite never blocks aiogram's
    event loop.  SQLite still guards cross-process transitions with ``BEGIN IMMEDIATE``.
    """

    def __init__(
        self,
        path: str | Path,
        *,
        default_expense_categories: Sequence[str] = DEFAULT_EXPENSE_CATEGORIES,
        default_income_categories: Sequence[str] = DEFAULT_INCOME_CATEGORIES,
    ) -> None:
        self.path = str(path)
        self.default_expense_categories = tuple(_normalize_categories(default_expense_categories))
        self.default_income_categories = tuple(_normalize_categories(default_income_categories))
        self._connection: sqlite3.Connection | None = None
        self._lock = asyncio.Lock()

    async def __aenter__(self) -> SQLiteState:
        await self.initialize()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    async def initialize(self) -> None:
        async with self._lock:
            if self._connection is not None:
                return
            self._connection = await asyncio.to_thread(self._open_and_initialize)

    def _open_and_initialize(self) -> sqlite3.Connection:
        if self.path != ":memory:" and not self.path.startswith("file:"):
            Path(self.path).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(
            self.path,
            timeout=30,
            isolation_level=None,
            check_same_thread=False,
            uri=self.path.startswith("file:"),
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        if self.path != ":memory:":
            connection.execute("PRAGMA journal_mode = WAL")
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                telegram_user_id INTEGER PRIMARY KEY,
                sheet_id TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS categories (
                telegram_user_id INTEGER NOT NULL,
                kind TEXT NOT NULL CHECK (kind IN ('expense', 'income')),
                name TEXT NOT NULL,
                position INTEGER NOT NULL,
                PRIMARY KEY (telegram_user_id, kind, name),
                FOREIGN KEY (telegram_user_id)
                    REFERENCES users(telegram_user_id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS categories_order_idx
                ON categories(telegram_user_id, kind, position);

            CREATE TABLE IF NOT EXISTS drafts (
                token TEXT PRIMARY KEY,
                telegram_user_id INTEGER NOT NULL,
                status TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (telegram_user_id)
                    REFERENCES users(telegram_user_id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS drafts_user_idx
                ON drafts(telegram_user_id, created_at DESC);
            CREATE INDEX IF NOT EXISTS drafts_expiry_idx ON drafts(expires_at);
            """
        )
        return connection

    async def close(self) -> None:
        async with self._lock:
            connection, self._connection = self._connection, None
            if connection is not None:
                await asyncio.to_thread(connection.close)

    def _require_connection(self) -> sqlite3.Connection:
        if self._connection is None:
            raise RuntimeError("SQLiteState.initialize() must be awaited before use")
        return self._connection

    async def _run(self, function: Any, /, *args: Any) -> Any:
        async with self._lock:
            connection = self._require_connection()
            return await asyncio.to_thread(function, connection, *args)

    async def ensure_user(self, telegram_user_id: int) -> None:
        await self._run(self._ensure_user_sync, telegram_user_id)

    def _ensure_user_sync(self, connection: sqlite3.Connection, telegram_user_id: int) -> None:
        now = datetime.now(UTC).isoformat()
        connection.execute("BEGIN IMMEDIATE")
        try:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO users(
                    telegram_user_id, sheet_id, created_at, updated_at
                ) VALUES (?, NULL, ?, ?)
                """,
                (telegram_user_id, now, now),
            )
            if cursor.rowcount:
                self._insert_categories_sync(
                    connection,
                    telegram_user_id,
                    "expense",
                    self.default_expense_categories,
                )
                self._insert_categories_sync(
                    connection,
                    telegram_user_id,
                    "income",
                    self.default_income_categories,
                )
            connection.execute("COMMIT")
        except BaseException:
            connection.execute("ROLLBACK")
            raise

    @staticmethod
    def _insert_categories_sync(
        connection: sqlite3.Connection,
        telegram_user_id: int,
        kind: str,
        values: Sequence[str],
    ) -> None:
        connection.executemany(
            """
            INSERT INTO categories(telegram_user_id, kind, name, position)
            VALUES (?, ?, ?, ?)
            """,
            [(telegram_user_id, kind, value, index) for index, value in enumerate(values)],
        )

    async def get_sheet_id(self, telegram_user_id: int) -> str | None:
        return await self._run(self._get_sheet_id_sync, telegram_user_id)

    @staticmethod
    def _get_sheet_id_sync(connection: sqlite3.Connection, telegram_user_id: int) -> str | None:
        row = connection.execute(
            "SELECT sheet_id FROM users WHERE telegram_user_id = ?",
            (telegram_user_id,),
        ).fetchone()
        return None if row is None else row["sheet_id"]

    async def connect_sheet(self, telegram_user_id: int, sheet_id: str) -> None:
        normalized = sheet_id.strip()
        if not normalized:
            raise ValueError("sheet_id cannot be empty")
        await self.ensure_user(telegram_user_id)
        await self._run(self._connect_sheet_sync, telegram_user_id, normalized)

    @staticmethod
    def _connect_sheet_sync(
        connection: sqlite3.Connection, telegram_user_id: int, sheet_id: str
    ) -> None:
        connection.execute(
            """
            UPDATE users SET sheet_id = ?, updated_at = ?
            WHERE telegram_user_id = ?
            """,
            (sheet_id, datetime.now(UTC).isoformat(), telegram_user_id),
        )

    async def get_categories(self, telegram_user_id: int) -> tuple[list[str], list[str]]:
        await self.ensure_user(telegram_user_id)
        return await self._run(self._get_categories_sync, telegram_user_id)

    @staticmethod
    def _get_categories_sync(
        connection: sqlite3.Connection, telegram_user_id: int
    ) -> tuple[list[str], list[str]]:
        rows = connection.execute(
            """
            SELECT kind, name FROM categories
            WHERE telegram_user_id = ?
            ORDER BY kind, position, rowid
            """,
            (telegram_user_id,),
        ).fetchall()
        expense = [row["name"] for row in rows if row["kind"] == "expense"]
        income = [row["name"] for row in rows if row["kind"] == "income"]
        return expense, income

    async def set_categories(
        self,
        telegram_user_id: int,
        expense: Sequence[str],
        income: Sequence[str],
    ) -> None:
        normalized_expense = _normalize_categories(expense)
        normalized_income = _normalize_categories(income)
        if not normalized_expense or not normalized_income:
            raise ValueError("expense and income category lists must not be empty")
        await self.ensure_user(telegram_user_id)
        await self._run(
            self._set_categories_sync,
            telegram_user_id,
            normalized_expense,
            normalized_income,
        )

    def _set_categories_sync(
        self,
        connection: sqlite3.Connection,
        telegram_user_id: int,
        expense: Sequence[str],
        income: Sequence[str],
    ) -> None:
        connection.execute("BEGIN IMMEDIATE")
        try:
            connection.execute(
                "DELETE FROM categories WHERE telegram_user_id = ?",
                (telegram_user_id,),
            )
            self._insert_categories_sync(connection, telegram_user_id, "expense", expense)
            self._insert_categories_sync(connection, telegram_user_id, "income", income)
            connection.execute(
                "UPDATE users SET updated_at = ? WHERE telegram_user_id = ?",
                (datetime.now(UTC).isoformat(), telegram_user_id),
            )
            connection.execute("COMMIT")
        except BaseException:
            connection.execute("ROLLBACK")
            raise

    async def create_draft(self, draft: DraftBatch) -> None:
        await self.ensure_user(draft.telegram_user_id)
        await self._run(self._create_draft_sync, draft)

    @staticmethod
    def _create_draft_sync(connection: sqlite3.Connection, draft: DraftBatch) -> None:
        connection.execute(
            """
            INSERT INTO drafts(
                token, telegram_user_id, status, payload_json,
                created_at, expires_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(token) DO UPDATE SET
                telegram_user_id = excluded.telegram_user_id,
                status = excluded.status,
                payload_json = excluded.payload_json,
                created_at = excluded.created_at,
                expires_at = excluded.expires_at,
                updated_at = excluded.updated_at
            """,
            (
                draft.token,
                draft.telegram_user_id,
                _status_value(draft.status),
                draft.model_dump_json(),
                _utc_iso(draft.created_at),
                _utc_iso(draft.expires_at),
                datetime.now(UTC).isoformat(),
            ),
        )

    async def get_draft(self, token: str, telegram_user_id: int) -> DraftBatch | None:
        return await self._run(self._get_draft_sync, token, telegram_user_id)

    @staticmethod
    def _get_draft_sync(
        connection: sqlite3.Connection, token: str, telegram_user_id: int
    ) -> DraftBatch | None:
        row = connection.execute(
            """
            SELECT payload_json, expires_at FROM drafts
            WHERE token = ? AND telegram_user_id = ?
            """,
            (token, telegram_user_id),
        ).fetchone()
        if row is None:
            return None
        if datetime.fromisoformat(row["expires_at"]) <= datetime.now(UTC):
            return None
        return DraftBatch.model_validate_json(row["payload_json"])

    async def save_draft(self, draft: DraftBatch) -> None:
        await self.ensure_user(draft.telegram_user_id)
        await self._run(self._save_draft_sync, draft)

    @staticmethod
    def _save_draft_sync(connection: sqlite3.Connection, draft: DraftBatch) -> None:
        cursor = connection.execute(
            """
            UPDATE drafts SET
                status = ?, payload_json = ?, created_at = ?,
                expires_at = ?, updated_at = ?
            WHERE token = ? AND telegram_user_id = ?
            """,
            (
                _status_value(draft.status),
                draft.model_dump_json(),
                _utc_iso(draft.created_at),
                _utc_iso(draft.expires_at),
                datetime.now(UTC).isoformat(),
                draft.token,
                draft.telegram_user_id,
            ),
        )
        if cursor.rowcount == 0:
            raise KeyError(f"draft {draft.token!r} does not exist")

    async def transition_draft(
        self,
        token: str,
        telegram_user_id: int,
        from_statuses: set[DraftStatus],
        to_status: DraftStatus,
    ) -> bool:
        if not from_statuses:
            return False
        return await self._run(
            self._transition_draft_sync,
            token,
            telegram_user_id,
            {_status_value(value) for value in from_statuses},
            to_status,
        )

    @staticmethod
    def _transition_draft_sync(
        connection: sqlite3.Connection,
        token: str,
        telegram_user_id: int,
        from_statuses: set[str],
        to_status: DraftStatus,
    ) -> bool:
        connection.execute("BEGIN IMMEDIATE")
        try:
            row = connection.execute(
                """
                SELECT status, payload_json, expires_at FROM drafts
                WHERE token = ? AND telegram_user_id = ?
                """,
                (token, telegram_user_id),
            ).fetchone()
            if (
                row is None
                or row["status"] not in from_statuses
                or datetime.fromisoformat(row["expires_at"]) <= datetime.now(UTC)
            ):
                connection.execute("ROLLBACK")
                return False

            draft = DraftBatch.model_validate_json(row["payload_json"])
            updated = draft.model_copy(update={"status": to_status})
            cursor = connection.execute(
                """
                UPDATE drafts SET status = ?, payload_json = ?, updated_at = ?
                WHERE token = ? AND telegram_user_id = ? AND status = ?
                """,
                (
                    _status_value(to_status),
                    updated.model_dump_json(),
                    datetime.now(UTC).isoformat(),
                    token,
                    telegram_user_id,
                    row["status"],
                ),
            )
            connection.execute("COMMIT")
            return cursor.rowcount == 1
        except BaseException:
            connection.execute("ROLLBACK")
            raise

    async def delete_draft(self, token: str, telegram_user_id: int) -> bool:
        return await self._run(self._delete_draft_sync, token, telegram_user_id)

    @staticmethod
    def _delete_draft_sync(
        connection: sqlite3.Connection, token: str, telegram_user_id: int
    ) -> bool:
        cursor = connection.execute(
            "DELETE FROM drafts WHERE token = ? AND telegram_user_id = ?",
            (token, telegram_user_id),
        )
        return cursor.rowcount == 1

    async def expire_drafts(self, now: datetime | None = None) -> int:
        cutoff = now or datetime.now(UTC)
        return await self._run(self._expire_drafts_sync, _utc_iso(cutoff))

    @staticmethod
    def _expire_drafts_sync(connection: sqlite3.Connection, cutoff_iso: str) -> int:
        cursor = connection.execute("DELETE FROM drafts WHERE expires_at <= ?", (cutoff_iso,))
        return cursor.rowcount
