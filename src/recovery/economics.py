"""Unit economics behind policy rule P7 (docs/DESIGN.md section 6).

Call only when expected incremental margin is at least `roi_floor` times the expected cost of trying.
"""

from dataclasses import dataclass
from decimal import Decimal

# USD per connected minute, list prices October 2026. Telephony depends on the destination.
VOICE_PER_MIN = Decimal("0.08")
LLM_PER_MIN = Decimal("0.02")
TELEPHONY_PER_MIN = {"AE": Decimal("0.30"), "SA": Decimal("0.31"), "US": Decimal("0.014")}
DEFAULT_TELEPHONY_PER_MIN = Decimal("0.30")


@dataclass(frozen=True)
class Assumptions:
    connect_rate: Decimal = Decimal("0.60")
    recover_given_connect: Decimal = Decimal("0.10")
    incremental_share: Decimal = Decimal("0.50")  # share of recoveries that would not have happened anyway
    gross_margin: Decimal = Decimal("0.40")
    connected_minutes: Decimal = Decimal("1.5")
    unconnected_minutes: Decimal = Decimal("0.4")  # voicemail and short calls
    attempts: Decimal = Decimal("1.4")  # average dials per called cart with up to 2 attempts
    roi_floor: Decimal = Decimal("3")


DEFAULT_ASSUMPTIONS = Assumptions()


def per_minute_cost(country: str) -> Decimal:
    return VOICE_PER_MIN + LLM_PER_MIN + TELEPHONY_PER_MIN.get(country, DEFAULT_TELEPHONY_PER_MIN)


def expected_cost(country: str, a: Assumptions = DEFAULT_ASSUMPTIONS) -> Decimal:
    minutes = a.connect_rate * a.connected_minutes + (1 - a.connect_rate) * a.unconnected_minutes
    return per_minute_cost(country) * minutes * a.attempts


def min_cart_value(country: str, a: Assumptions = DEFAULT_ASSUMPTIONS) -> Decimal:
    """Smallest cart (USD) where expected incremental margin covers roi_floor x expected cost."""
    margin_per_dollar = a.connect_rate * a.recover_given_connect * a.incremental_share * a.gross_margin
    return (a.roi_floor * expected_cost(country, a) / margin_per_dollar).quantize(Decimal("1"))
