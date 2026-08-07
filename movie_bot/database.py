from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypeVar

from .models import GuildSettings, MovieOrder, OwnerShareSummary, PaymentMethod
from .pricing import (
    DEFAULT_DISCOUNT_BASIS_POINTS,
    discounted_price_cents,
    percentage_share_cents,
)

T = TypeVar("T")

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS guild_settings (
    guild_id INTEGER PRIMARY KEY,
    brand_name TEXT NOT NULL,
    ticket_category_id INTEGER NOT NULL,
    staff_role_id INTEGER NOT NULL,
    notification_role_id INTEGER,
    log_channel_id INTEGER NOT NULL,
    banner_url TEXT,
    panel_channel_id INTEGER,
    panel_message_id INTEGER,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS movie_orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    customer_id INTEGER NOT NULL,
    channel_id INTEGER UNIQUE,
    movie_showtime TEXT NOT NULL,
    zip_code TEXT NOT NULL,
    seats INTEGER NOT NULL CHECK (seats BETWEEN 1 AND 20),
    snacks TEXT NOT NULL,
    submitted_total_cents INTEGER NOT NULL CHECK (submitted_total_cents > 0),
    customer_price_cents INTEGER NOT NULL CHECK (customer_price_cents >= 0),
    discount_basis_points INTEGER NOT NULL DEFAULT 5000,
    assigned_staff_id INTEGER,
    payment_method TEXT,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    closed_at TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS one_active_movie_order_per_customer
ON movie_orders(guild_id, customer_id)
WHERE status NOT IN ('closed', 'cancelled');

CREATE TABLE IF NOT EXISTS payment_methods (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    staff_user_id INTEGER NOT NULL,
    name TEXT NOT NULL COLLATE NOCASE,
    instructions TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(guild_id, staff_user_id, name)
);

CREATE TABLE IF NOT EXISTS store_status (
    guild_id INTEGER PRIMARY KEY,
    orders_open INTEGER NOT NULL DEFAULT 1 CHECK (orders_open IN (0, 1)),
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS application_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS owner_shares (
    order_id INTEGER PRIMARY KEY,
    guild_id INTEGER NOT NULL,
    order_total_cents INTEGER NOT NULL CHECK (order_total_cents > 0),
    share_percent INTEGER NOT NULL CHECK (share_percent BETWEEN 1 AND 100),
    share_cents INTEGER NOT NULL CHECK (share_cents > 0),
    status TEXT NOT NULL DEFAULT 'owed' CHECK (status IN ('owed', 'paid')),
    recorded_by INTEGER NOT NULL,
    recorded_at TEXT NOT NULL,
    settled_by INTEGER,
    settled_at TEXT,
    FOREIGN KEY(order_id) REFERENCES movie_orders(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS owner_shares_by_guild_status
ON owner_shares(guild_id, status, recorded_at);

CREATE TABLE IF NOT EXISTS order_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id INTEGER NOT NULL,
    actor_id INTEGER,
    event_type TEXT NOT NULL,
    details_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(order_id) REFERENCES movie_orders(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS movie_orders_by_channel ON movie_orders(channel_id);
CREATE INDEX IF NOT EXISTS movie_events_by_order ON order_events(order_id, id);
"""


class ActiveOrderExistsError(RuntimeError):
    """Raised when a customer already has an open movie order."""


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _settings_from_row(row: sqlite3.Row | None) -> GuildSettings | None:
    if row is None:
        return None
    return GuildSettings(
        guild_id=row["guild_id"],
        brand_name=row["brand_name"],
        ticket_category_id=row["ticket_category_id"],
        staff_role_id=row["staff_role_id"],
        notification_role_id=row["notification_role_id"],
        log_channel_id=row["log_channel_id"],
        banner_url=row["banner_url"],
        panel_channel_id=row["panel_channel_id"],
        panel_message_id=row["panel_message_id"],
    )


def _order_from_row(row: sqlite3.Row | None) -> MovieOrder | None:
    if row is None:
        return None
    return MovieOrder(
        id=row["id"],
        guild_id=row["guild_id"],
        customer_id=row["customer_id"],
        channel_id=row["channel_id"],
        movie_showtime=row["movie_showtime"],
        zip_code=row["zip_code"],
        seats=row["seats"],
        snacks=row["snacks"],
        submitted_total_cents=row["submitted_total_cents"],
        customer_price_cents=row["customer_price_cents"],
        discount_basis_points=row["discount_basis_points"],
        assigned_staff_id=row["assigned_staff_id"],
        payment_method=row["payment_method"],
        status=row["status"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _payment_from_row(row: sqlite3.Row) -> PaymentMethod:
    return PaymentMethod(
        id=row["id"],
        guild_id=row["guild_id"],
        staff_user_id=row["staff_user_id"],
        name=row["name"],
        instructions=row["instructions"],
        enabled=bool(row["enabled"]),
    )


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._write_lock = asyncio.Lock()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    async def _run(self, operation: Callable[[], T]) -> T:
        return await asyncio.to_thread(operation)

    async def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)

        def operation() -> None:
            with self._connect() as connection:
                connection.execute("PRAGMA journal_mode = WAL")
                connection.executescript(SCHEMA)

        async with self._write_lock:
            await self._run(operation)

    async def get_application_state(self, key: str) -> str | None:
        def operation() -> str | None:
            with self._connect() as connection:
                row = connection.execute(
                    "SELECT value FROM application_state WHERE key = ?", (key,)
                ).fetchone()
                return None if row is None else str(row["value"])

        return await self._run(operation)

    async def set_application_state(self, key: str, value: str) -> None:
        def operation() -> None:
            with self._connect() as connection:
                connection.execute(
                    """
                    INSERT INTO application_state (key, value, updated_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(key) DO UPDATE SET
                        value = excluded.value,
                        updated_at = excluded.updated_at
                    """,
                    (key, value, _now()),
                )

        async with self._write_lock:
            await self._run(operation)

    async def upsert_guild_settings(
        self,
        *,
        guild_id: int,
        brand_name: str,
        ticket_category_id: int,
        staff_role_id: int,
        notification_role_id: int | None,
        log_channel_id: int,
        banner_url: str | None,
    ) -> None:
        now = _now()

        def operation() -> None:
            with self._connect() as connection:
                connection.execute(
                    """
                    INSERT INTO guild_settings (
                        guild_id, brand_name, ticket_category_id, staff_role_id,
                        notification_role_id, log_channel_id, banner_url,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(guild_id) DO UPDATE SET
                        brand_name = excluded.brand_name,
                        ticket_category_id = excluded.ticket_category_id,
                        staff_role_id = excluded.staff_role_id,
                        notification_role_id = excluded.notification_role_id,
                        log_channel_id = excluded.log_channel_id,
                        banner_url = excluded.banner_url,
                        updated_at = excluded.updated_at
                    """,
                    (
                        guild_id,
                        brand_name,
                        ticket_category_id,
                        staff_role_id,
                        notification_role_id,
                        log_channel_id,
                        banner_url,
                        now,
                        now,
                    ),
                )

        async with self._write_lock:
            await self._run(operation)

    async def get_guild_settings(self, guild_id: int) -> GuildSettings | None:
        def operation() -> GuildSettings | None:
            with self._connect() as connection:
                row = connection.execute(
                    "SELECT * FROM guild_settings WHERE guild_id = ?", (guild_id,)
                ).fetchone()
                return _settings_from_row(row)

        return await self._run(operation)

    async def save_panel(self, guild_id: int, channel_id: int, message_id: int) -> None:
        def operation() -> None:
            with self._connect() as connection:
                connection.execute(
                    """
                    UPDATE guild_settings
                    SET panel_channel_id = ?, panel_message_id = ?, updated_at = ?
                    WHERE guild_id = ?
                    """,
                    (channel_id, message_id, _now(), guild_id),
                )

        async with self._write_lock:
            await self._run(operation)

    async def get_store_open(self, guild_id: int) -> bool:
        def operation() -> bool:
            with self._connect() as connection:
                row = connection.execute(
                    "SELECT orders_open FROM store_status WHERE guild_id = ?",
                    (guild_id,),
                ).fetchone()
                return True if row is None else bool(row["orders_open"])

        return await self._run(operation)

    async def set_store_open(self, guild_id: int, orders_open: bool) -> None:
        def operation() -> None:
            with self._connect() as connection:
                connection.execute(
                    """
                    INSERT INTO store_status (guild_id, orders_open, updated_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(guild_id) DO UPDATE SET
                        orders_open = excluded.orders_open,
                        updated_at = excluded.updated_at
                    """,
                    (guild_id, int(orders_open), _now()),
                )

        async with self._write_lock:
            await self._run(operation)

    async def create_order(
        self,
        *,
        guild_id: int,
        customer_id: int,
        movie_showtime: str,
        zip_code: str,
        seats: int,
        snacks: str,
        submitted_total_cents: int,
    ) -> MovieOrder:
        now = _now()
        customer_price = discounted_price_cents(submitted_total_cents)

        def operation() -> MovieOrder:
            try:
                with self._connect() as connection:
                    cursor = connection.execute(
                        """
                        INSERT INTO movie_orders (
                            guild_id, customer_id, movie_showtime, zip_code, seats,
                            snacks, submitted_total_cents, customer_price_cents,
                            discount_basis_points, status, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            guild_id,
                            customer_id,
                            movie_showtime,
                            zip_code,
                            seats,
                            snacks,
                            submitted_total_cents,
                            customer_price,
                            DEFAULT_DISCOUNT_BASIS_POINTS,
                            "creating",
                            now,
                            now,
                        ),
                    )
                    row = connection.execute(
                        "SELECT * FROM movie_orders WHERE id = ?", (cursor.lastrowid,)
                    ).fetchone()
            except sqlite3.IntegrityError as exc:
                text = str(exc)
                if (
                    "one_active_movie_order_per_customer" in text
                    or "movie_orders.guild_id, movie_orders.customer_id" in text
                ):
                    raise ActiveOrderExistsError from exc
                raise
            order = _order_from_row(row)
            assert order is not None
            return order

        async with self._write_lock:
            return await self._run(operation)

    async def attach_channel(self, order_id: int, channel_id: int) -> MovieOrder:
        def operation() -> MovieOrder:
            with self._connect() as connection:
                connection.execute(
                    """
                    UPDATE movie_orders
                    SET channel_id = ?, status = 'open', updated_at = ?
                    WHERE id = ?
                    """,
                    (channel_id, _now(), order_id),
                )
                row = connection.execute(
                    "SELECT * FROM movie_orders WHERE id = ?", (order_id,)
                ).fetchone()
            order = _order_from_row(row)
            assert order is not None
            return order

        async with self._write_lock:
            return await self._run(operation)

    async def cancel_order_creation(self, order_id: int) -> None:
        await self.set_order_status(order_id, "cancelled", actor_id=None)

    async def get_order_by_channel(self, channel_id: int) -> MovieOrder | None:
        def operation() -> MovieOrder | None:
            with self._connect() as connection:
                row = connection.execute(
                    "SELECT * FROM movie_orders WHERE channel_id = ?", (channel_id,)
                ).fetchone()
                return _order_from_row(row)

        return await self._run(operation)

    async def get_active_order_for_customer(
        self, guild_id: int, customer_id: int
    ) -> MovieOrder | None:
        def operation() -> MovieOrder | None:
            with self._connect() as connection:
                row = connection.execute(
                    """
                    SELECT * FROM movie_orders
                    WHERE guild_id = ? AND customer_id = ?
                      AND status NOT IN ('closed', 'cancelled')
                    ORDER BY id DESC LIMIT 1
                    """,
                    (guild_id, customer_id),
                ).fetchone()
                return _order_from_row(row)

        return await self._run(operation)

    async def assign_order(self, order_id: int, staff_user_id: int) -> MovieOrder:
        def operation() -> MovieOrder:
            with self._connect() as connection:
                connection.execute(
                    """
                    UPDATE movie_orders
                    SET assigned_staff_id = ?, updated_at = ? WHERE id = ?
                    """,
                    (staff_user_id, _now(), order_id),
                )
                row = connection.execute(
                    "SELECT * FROM movie_orders WHERE id = ?", (order_id,)
                ).fetchone()
            order = _order_from_row(row)
            assert order is not None
            return order

        async with self._write_lock:
            order = await self._run(operation)
        await self.add_event(order_id, "claimed", actor_id=staff_user_id)
        return order

    async def update_order_total(
        self, order_id: int, total_cents: int, *, actor_id: int
    ) -> MovieOrder:
        customer_price = discounted_price_cents(total_cents)

        def operation() -> MovieOrder:
            with self._connect() as connection:
                connection.execute(
                    """
                    UPDATE movie_orders
                    SET submitted_total_cents = ?, customer_price_cents = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (total_cents, customer_price, _now(), order_id),
                )
                row = connection.execute(
                    "SELECT * FROM movie_orders WHERE id = ?", (order_id,)
                ).fetchone()
            order = _order_from_row(row)
            assert order is not None
            return order

        async with self._write_lock:
            order = await self._run(operation)
        await self.add_event(
            order_id,
            "total_corrected",
            actor_id=actor_id,
            details={"submitted_total_cents": total_cents},
        )
        return order

    async def set_order_payment_method(
        self, order_id: int, method: str, *, actor_id: int
    ) -> MovieOrder:
        def operation() -> MovieOrder:
            with self._connect() as connection:
                connection.execute(
                    """
                    UPDATE movie_orders
                    SET payment_method = ?, updated_at = ? WHERE id = ?
                    """,
                    (method, _now(), order_id),
                )
                row = connection.execute(
                    "SELECT * FROM movie_orders WHERE id = ?", (order_id,)
                ).fetchone()
            order = _order_from_row(row)
            assert order is not None
            return order

        async with self._write_lock:
            order = await self._run(operation)
        await self.add_event(
            order_id,
            "payment_method_selected",
            actor_id=actor_id,
            details={"method": method},
        )
        return order

    async def set_order_status(
        self,
        order_id: int,
        status: str,
        *,
        actor_id: int | None,
        details: dict[str, Any] | None = None,
    ) -> MovieOrder:
        closed_at = _now() if status in {"closed", "cancelled"} else None

        def operation() -> MovieOrder:
            with self._connect() as connection:
                connection.execute(
                    """
                    UPDATE movie_orders
                    SET status = ?, updated_at = ?,
                        closed_at = CASE WHEN ? IS NULL THEN closed_at ELSE ? END
                    WHERE id = ?
                    """,
                    (status, _now(), closed_at, closed_at, order_id),
                )
                row = connection.execute(
                    "SELECT * FROM movie_orders WHERE id = ?", (order_id,)
                ).fetchone()
            order = _order_from_row(row)
            assert order is not None
            return order

        async with self._write_lock:
            order = await self._run(operation)
        await self.add_event(order_id, status, actor_id=actor_id, details=details)
        return order

    async def close_order_with_owner_share(
        self,
        order_id: int,
        *,
        guild_id: int,
        share_percent: int,
        actor_id: int,
        reason: str,
    ) -> tuple[MovieOrder, bool, int]:
        now = _now()

        def operation() -> tuple[MovieOrder, bool, int]:
            with self._connect() as connection:
                existing = connection.execute(
                    "SELECT guild_id, submitted_total_cents FROM movie_orders WHERE id = ?",
                    (order_id,),
                ).fetchone()
                if existing is None:
                    raise ValueError(f"Movie order {order_id} does not exist.")
                if int(existing["guild_id"]) != guild_id:
                    raise ValueError("Movie order does not belong to this Discord server.")

                order_total_cents = int(existing["submitted_total_cents"])
                share_cents = percentage_share_cents(order_total_cents, share_percent)
                cursor = connection.execute(
                    """
                    INSERT OR IGNORE INTO owner_shares (
                        order_id, guild_id, order_total_cents, share_percent,
                        share_cents, status, recorded_by, recorded_at
                    ) VALUES (?, ?, ?, ?, ?, 'owed', ?, ?)
                    """,
                    (
                        order_id,
                        guild_id,
                        order_total_cents,
                        share_percent,
                        share_cents,
                        actor_id,
                        now,
                    ),
                )
                share_recorded = cursor.rowcount > 0
                connection.execute(
                    """
                    UPDATE movie_orders
                    SET status = 'closed', updated_at = ?,
                        closed_at = COALESCE(closed_at, ?)
                    WHERE id = ?
                    """,
                    (now, now, order_id),
                )
                connection.execute(
                    """
                    INSERT INTO order_events (
                        order_id, actor_id, event_type, details_json, created_at
                    ) VALUES (?, ?, 'completed_with_owner_share', ?, ?)
                    """,
                    (
                        order_id,
                        actor_id,
                        json.dumps(
                            {
                                "reason": reason,
                                "order_total_cents": order_total_cents,
                                "owner_share_percent": share_percent,
                                "owner_share_cents": share_cents,
                                "share_recorded": share_recorded,
                            },
                            separators=(",", ":"),
                            sort_keys=True,
                        ),
                        now,
                    ),
                )
                row = connection.execute(
                    "SELECT * FROM movie_orders WHERE id = ?", (order_id,)
                ).fetchone()

            order = _order_from_row(row)
            assert order is not None
            return order, share_recorded, share_cents

        async with self._write_lock:
            return await self._run(operation)

    async def get_owner_share_summary(self, guild_id: int) -> OwnerShareSummary:
        def operation() -> OwnerShareSummary:
            with self._connect() as connection:
                row = connection.execute(
                    """
                    SELECT
                        COUNT(*) AS lifetime_order_count,
                        COALESCE(SUM(share_cents), 0) AS lifetime_cents,
                        COALESCE(SUM(CASE WHEN status = 'owed' THEN 1 ELSE 0 END), 0)
                            AS owed_order_count,
                        COALESCE(SUM(CASE WHEN status = 'owed' THEN share_cents ELSE 0 END), 0)
                            AS owed_cents
                    FROM owner_shares
                    WHERE guild_id = ?
                    """,
                    (guild_id,),
                ).fetchone()
                assert row is not None
                return OwnerShareSummary(
                    owed_order_count=int(row["owed_order_count"]),
                    owed_cents=int(row["owed_cents"]),
                    lifetime_order_count=int(row["lifetime_order_count"]),
                    lifetime_cents=int(row["lifetime_cents"]),
                )

        return await self._run(operation)

    async def add_event(
        self,
        order_id: int,
        event_type: str,
        *,
        actor_id: int | None,
        details: dict[str, Any] | None = None,
    ) -> None:
        def operation() -> None:
            with self._connect() as connection:
                connection.execute(
                    """
                    INSERT INTO order_events (
                        order_id, actor_id, event_type, details_json, created_at
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (order_id, actor_id, event_type, json.dumps(details or {}), _now()),
                )

        async with self._write_lock:
            await self._run(operation)

    async def upsert_payment_method(
        self, *, guild_id: int, staff_user_id: int, name: str, instructions: str
    ) -> None:
        now = _now()

        def operation() -> None:
            with self._connect() as connection:
                connection.execute(
                    """
                    INSERT INTO payment_methods (
                        guild_id, staff_user_id, name, instructions,
                        enabled, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, 1, ?, ?)
                    ON CONFLICT(guild_id, staff_user_id, name) DO UPDATE SET
                        instructions = excluded.instructions,
                        enabled = 1,
                        updated_at = excluded.updated_at
                    """,
                    (guild_id, staff_user_id, name, instructions, now, now),
                )

        async with self._write_lock:
            await self._run(operation)

    async def list_payment_methods(
        self, guild_id: int, staff_user_id: int
    ) -> list[PaymentMethod]:
        def operation() -> list[PaymentMethod]:
            with self._connect() as connection:
                rows = connection.execute(
                    """
                    SELECT * FROM payment_methods
                    WHERE guild_id = ? AND staff_user_id = ? AND enabled = 1
                    ORDER BY name COLLATE NOCASE
                    """,
                    (guild_id, staff_user_id),
                ).fetchall()
                return [_payment_from_row(row) for row in rows]

        return await self._run(operation)

    async def remove_payment_method(self, guild_id: int, staff_user_id: int, name: str) -> bool:
        def operation() -> bool:
            with self._connect() as connection:
                cursor = connection.execute(
                    """
                    UPDATE payment_methods SET enabled = 0, updated_at = ?
                    WHERE guild_id = ? AND staff_user_id = ? AND name = ? COLLATE NOCASE
                    """,
                    (_now(), guild_id, staff_user_id, name),
                )
                return cursor.rowcount > 0

        async with self._write_lock:
            return await self._run(operation)
