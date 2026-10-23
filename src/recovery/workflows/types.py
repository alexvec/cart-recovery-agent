from dataclasses import dataclass
from typing import Any


@dataclass
class RecoveryInput:
    shop: str
    checkout_token: str
    call_delay_minutes: int = 20
    max_attempts: int = 2
    attribution_window_hours: int = 72


@dataclass
class PolicyResult:
    allowed: bool
    reason: str
    retry_at: str | None = None  # ISO timestamp


@dataclass
class CallOutcome:
    outcome: str  # completed | no_answer | busy | failed | timeout
    conversation_id: str | None = None
    duration_s: int | None = None


@dataclass
class OrderInfo:
    order_id: int
    value: str  # Decimal as string, in shop currency


@dataclass
class EventIn:
    workflow_id: str
    shop: str
    type: str
    data: dict[str, Any]


def workflow_id_for(shop: str, checkout_token: str) -> str:
    return f"recover:{shop}:{checkout_token}"
