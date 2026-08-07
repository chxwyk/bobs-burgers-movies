from __future__ import annotations

import re
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

MONEY_PATTERN = re.compile(r"^\$?\s*(\d{1,3}(?:,\d{3})*|\d+)(?:\.(\d{1,2}))?\s*$")
ZIP_PATTERN = re.compile(r"^\d{5}(?:-\d{4})?$")
MIN_ORDER_TOTAL_CENTS = 4_000
MAX_ORDER_TOTAL_CENTS = 25_000
MIN_SEATS = 1
MAX_SEATS = 20
DEFAULT_DISCOUNT_BASIS_POINTS = 5_000


class InputError(ValueError):
    """Raised when customer order input cannot be safely interpreted."""


def parse_money(value: str) -> int:
    cleaned = value.strip()
    if not MONEY_PATTERN.fullmatch(cleaned):
        raise InputError("Enter a valid final total such as 64.50 or $64.50.")
    try:
        amount = Decimal(cleaned.replace("$", "").replace(",", "").strip())
        cents = int((amount * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    except (InvalidOperation, ValueError) as exc:
        raise InputError("Enter a valid dollar amount.") from exc
    if cents < MIN_ORDER_TOTAL_CENTS:
        raise InputError("The minimum final checkout total is $40.00.")
    if cents > MAX_ORDER_TOTAL_CENTS:
        raise InputError("The maximum final checkout total is $250.00.")
    return cents


def parse_seats(value: str) -> int:
    try:
        seats = int(value.strip())
    except ValueError as exc:
        raise InputError("Enter the number of seats as a whole number.") from exc
    if not MIN_SEATS <= seats <= MAX_SEATS:
        raise InputError(f"Seat count must be between {MIN_SEATS} and {MAX_SEATS}.")
    return seats


def validate_zip_code(value: str) -> str:
    cleaned = value.strip()
    if not ZIP_PATTERN.fullmatch(cleaned):
        raise InputError("Enter a valid 5-digit ZIP code, such as 89109.")
    return cleaned


def discounted_price_cents(
    total_cents: int,
    discount_basis_points: int = DEFAULT_DISCOUNT_BASIS_POINTS,
) -> int:
    if total_cents < 0:
        raise InputError("The total cannot be negative.")
    if not 0 <= discount_basis_points <= 10_000:
        raise ValueError("Discount basis points must be between 0 and 10,000.")
    customer_basis_points = 10_000 - discount_basis_points
    return (total_cents * customer_basis_points + 5_000) // 10_000


def percentage_share_cents(total_cents: int, percent: int) -> int:
    if total_cents < 0:
        raise InputError("The total cannot be negative.")
    if not 1 <= percent <= 100:
        raise ValueError("The percentage must be between 1 and 100.")
    return (total_cents * percent + 50) // 100


def format_cents(cents: int) -> str:
    sign = "-" if cents < 0 else ""
    absolute = abs(cents)
    return f"{sign}${absolute // 100:,}.{absolute % 100:02d}"
