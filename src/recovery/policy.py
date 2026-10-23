"""Pre-call policy: deterministic rules that decide whether a checkout may be called.

No LLM is involved here. Every decision carries a reason code so it can be counted,
alerted on and audited (see docs/DESIGN.md section 5.1).
"""

import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from zoneinfo import ZoneInfo


class Reason(StrEnum):
    ALLOWED = "allowed"
    KILL_SWITCH = "kill_switch"  # P1
    ALREADY_PURCHASED = "already_purchased"  # P2
    NO_CONSENT = "no_consent"  # P3
    DO_NOT_CALL = "do_not_call"  # P4
    NO_PHONE = "no_phone"
    OUTSIDE_CALLING_HOURS = "outside_calling_hours"  # P5
    FREQUENCY_CAP = "frequency_cap"  # P6
    BELOW_VALUE_THRESHOLD = "below_value_threshold"  # P7
    OUT_OF_STOCK = "out_of_stock"  # P7
    BUDGET_EXHAUSTED = "budget_exhausted"  # P8
    TEST_OR_RISKY = "test_or_risky"  # P9
    HOLDOUT = "holdout"  # P10


# Local calling hours as [start, end) in the callee's timezone.
# UAE: Cabinet Resolutions 56/57 of 2024. US: TCPA 8am-9pm. Unknown countries get the strictest window.
CALLING_HOURS: dict[str, tuple[int, int]] = {
    "AE": (9, 18),
    "US": (8, 21),
    "CA": (8, 21),
    "GB": (9, 20),
}
DEFAULT_CALLING_HOURS = (9, 18)

MAX_ATTEMPTS_PER_DAY = 1
MAX_ATTEMPTS_PER_WEEK = 2


@dataclass(frozen=True)
class PolicyInput:
    now: datetime  # timezone-aware
    kill_switch: bool
    order_exists: bool
    has_phone: bool
    consent: bool
    on_do_not_call: bool
    country: str  # ISO 3166-1 alpha-2 of the phone number
    timezones: tuple[str, ...]  # candidate IANA zones for the phone number
    attempts_last_24h: int
    attempts_last_7d: int
    cart_value: Decimal
    min_cart_value: Decimal
    in_stock: bool
    spend_today: Decimal
    daily_budget: Decimal
    is_test_or_risky: bool
    in_holdout: bool


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: Reason
    retry_at: datetime | None = None  # set when waiting would make the call allowed


def in_calling_hours(now: datetime, country: str, timezones: tuple[str, ...]) -> bool:
    """True only if `now` is inside calling hours in every candidate timezone.

    A US number can map to several zones; being conservative means never calling anyone too early or late.
    """
    start, end = CALLING_HOURS.get(country, DEFAULT_CALLING_HOURS)
    zones = timezones or ("UTC",)
    return all(start <= now.astimezone(ZoneInfo(z)).hour < end for z in zones)


def next_calling_window(now: datetime, country: str, timezones: tuple[str, ...]) -> datetime | None:
    """Earliest time (in 15-minute steps, up to 3 days ahead) when calling hours are open everywhere."""
    t = now.replace(second=0, microsecond=0) + timedelta(minutes=15 - now.minute % 15)
    for _ in range(3 * 24 * 4):
        if in_calling_hours(t, country, timezones):
            return t
        t += timedelta(minutes=15)
    return None


def in_holdout(shop: str, checkout_token: str, holdout_pct: int) -> bool:
    """Stable, reproducible random assignment: the same checkout always lands in the same group."""
    digest = hashlib.sha256(f"{shop}:{checkout_token}".encode()).digest()
    return int.from_bytes(digest[:8], "big") % 100 < holdout_pct


def evaluate(p: PolicyInput) -> Decision:
    """Apply rules in order; the first failing rule decides. Cheap and absolute rules come first."""
    if p.kill_switch:
        return Decision(False, Reason.KILL_SWITCH)
    if p.order_exists:
        return Decision(False, Reason.ALREADY_PURCHASED)
    if not p.has_phone:
        return Decision(False, Reason.NO_PHONE)
    if not p.consent:
        return Decision(False, Reason.NO_CONSENT)
    if p.on_do_not_call:
        return Decision(False, Reason.DO_NOT_CALL)
    if p.is_test_or_risky:
        return Decision(False, Reason.TEST_OR_RISKY)
    if not p.in_stock:
        return Decision(False, Reason.OUT_OF_STOCK)
    if p.cart_value < p.min_cart_value:
        return Decision(False, Reason.BELOW_VALUE_THRESHOLD)
    if p.attempts_last_24h >= MAX_ATTEMPTS_PER_DAY or p.attempts_last_7d >= MAX_ATTEMPTS_PER_WEEK:
        return Decision(False, Reason.FREQUENCY_CAP)
    if p.spend_today >= p.daily_budget:
        return Decision(False, Reason.BUDGET_EXHAUSTED)
    # Holdout is decided before the calling-hours wait so the treated and control groups
    # are drawn from exactly the same eligible population.
    if p.in_holdout:
        return Decision(False, Reason.HOLDOUT)
    if not in_calling_hours(p.now, p.country, p.timezones):
        return Decision(
            False, Reason.OUTSIDE_CALLING_HOURS, next_calling_window(p.now, p.country, p.timezones)
        )
    return Decision(True, Reason.ALLOWED)
