from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class GuildSettings:
    guild_id: int
    brand_name: str
    ticket_category_id: int
    staff_role_id: int
    notification_role_id: int | None
    log_channel_id: int
    banner_url: str | None
    panel_channel_id: int | None = None
    panel_message_id: int | None = None


@dataclass(frozen=True, slots=True)
class MovieOrder:
    id: int
    guild_id: int
    customer_id: int
    channel_id: int | None
    movie_showtime: str
    zip_code: str
    seats: int
    snacks: str
    submitted_total_cents: int
    customer_price_cents: int
    discount_basis_points: int
    assigned_staff_id: int | None
    payment_method: str | None
    status: str
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class PaymentMethod:
    id: int
    guild_id: int
    staff_user_id: int
    name: str
    instructions: str
    enabled: bool


@dataclass(frozen=True, slots=True)
class OwnerShareSummary:
    owed_order_count: int
    owed_cents: int
    lifetime_order_count: int
    lifetime_cents: int
