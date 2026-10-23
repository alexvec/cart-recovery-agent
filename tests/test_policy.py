from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from hypothesis import given
from hypothesis import strategies as st

from recovery import economics
from recovery.policy import (
    CALLING_HOURS,
    DEFAULT_CALLING_HOURS,
    PolicyInput,
    Reason,
    evaluate,
    in_calling_hours,
    in_holdout,
    next_calling_window,
)

NOON_DUBAI = datetime(2026, 10, 21, 8, 0, tzinfo=UTC)  # 12:00 in Dubai

BASE = PolicyInput(
    now=NOON_DUBAI,
    kill_switch=False,
    order_exists=False,
    has_phone=True,
    consent=True,
    on_do_not_call=False,
    country="AE",
    timezones=("Asia/Dubai",),
    attempts_last_24h=0,
    attempts_last_7d=0,
    cart_value=Decimal("300"),
    min_cart_value=Decimal("148"),
    in_stock=True,
    spend_today=Decimal("0"),
    daily_budget=Decimal("20"),
    is_test_or_risky=False,
    in_holdout=False,
)

ZONES = st.sampled_from(
    [
        ("Asia/Dubai",),
        ("Asia/Riyadh",),
        ("America/New_York",),
        ("America/New_York", "America/Chicago", "America/Los_Angeles"),
        ("Europe/London",),
    ]
)
COUNTRIES = st.sampled_from(["AE", "US", "GB", "SA", "XX"])
TIMES = st.datetimes(min_value=datetime(2026, 1, 1), max_value=datetime(2027, 12, 31), timezones=st.just(UTC))


def test_happy_path_is_allowed() -> None:
    assert evaluate(BASE).allowed


def test_each_blocking_rule_has_its_reason() -> None:
    cases = {
        Reason.KILL_SWITCH: {"kill_switch": True},
        Reason.ALREADY_PURCHASED: {"order_exists": True},
        Reason.NO_PHONE: {"has_phone": False},
        Reason.NO_CONSENT: {"consent": False},
        Reason.DO_NOT_CALL: {"on_do_not_call": True},
        Reason.TEST_OR_RISKY: {"is_test_or_risky": True},
        Reason.OUT_OF_STOCK: {"in_stock": False},
        Reason.BELOW_VALUE_THRESHOLD: {"cart_value": Decimal("100")},
        Reason.FREQUENCY_CAP: {"attempts_last_24h": 1},
        Reason.BUDGET_EXHAUSTED: {"spend_today": Decimal("20")},
        Reason.HOLDOUT: {"in_holdout": True},
    }
    for reason, change in cases.items():
        d = evaluate(replace(BASE, **change))  # type: ignore[arg-type]
        assert (d.allowed, d.reason) == (False, reason)


def test_weekly_cap() -> None:
    assert evaluate(replace(BASE, attempts_last_7d=2)).reason == Reason.FREQUENCY_CAP


def test_late_evening_in_dubai_waits_for_next_morning() -> None:
    d = evaluate(replace(BASE, now=datetime(2026, 10, 21, 15, 30, tzinfo=UTC)))  # 19:30 Dubai
    assert d.reason == Reason.OUTSIDE_CALLING_HOURS
    assert d.retry_at is not None
    assert d.retry_at.astimezone(ZoneInfo("Asia/Dubai")).hour == 9


@given(now=TIMES, zones=ZONES, country=COUNTRIES)
def test_never_allowed_outside_calling_hours_in_any_zone(
    now: datetime, zones: tuple[str, ...], country: str
) -> None:
    """The compliance property: an allowed call is inside calling hours in every candidate timezone."""
    d = evaluate(replace(BASE, now=now, timezones=zones, country=country))
    if d.allowed:
        start, end = CALLING_HOURS.get(country, DEFAULT_CALLING_HOURS)
        for z in zones:
            assert start <= now.astimezone(ZoneInfo(z)).hour < end


@given(now=TIMES, zones=ZONES, country=COUNTRIES)
def test_next_window_is_open_and_in_the_future(now: datetime, zones: tuple[str, ...], country: str) -> None:
    t = next_calling_window(now, country, zones)
    if t is not None:
        assert t > now and t - now <= timedelta(days=3)
        assert in_calling_hours(t, country, zones)


@given(st.booleans(), st.booleans(), st.booleans())
def test_hard_blocks_are_never_overridden(kill: bool, dnc: bool, consent: bool) -> None:
    d = evaluate(replace(BASE, kill_switch=kill, on_do_not_call=dnc, consent=consent))
    if kill or dnc or not consent:
        assert not d.allowed


def test_holdout_is_stable_and_close_to_target_share() -> None:
    assert in_holdout("s.myshopify.com", "abc", 15) == in_holdout("s.myshopify.com", "abc", 15)
    share = sum(in_holdout("s.myshopify.com", f"tok{i}", 15) for i in range(20_000)) / 20_000
    assert 0.14 < share < 0.16


def test_break_even_thresholds_match_the_design_doc() -> None:
    # docs/DESIGN.md section 6: about $140+ in the UAE, about $40+ in the US
    assert 130 <= economics.min_cart_value("AE") <= 160
    assert 35 <= economics.min_cart_value("US") <= 50
