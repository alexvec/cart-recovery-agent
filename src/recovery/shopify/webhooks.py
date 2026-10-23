"""Shopify webhook verification and payload normalization."""

import base64
import hashlib
import hmac
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from recovery.pii import normalize_phone

# Fixed or pegged rates are enough to compare carts against a USD threshold.
USD_RATES = {"USD": Decimal("1"), "AED": Decimal("0.2723"), "SAR": Decimal("0.2667"), "EUR": Decimal("1.08")}


def verify_hmac(raw_body: bytes, header_hmac: str | None, secret: str) -> bool:
    if not header_hmac or not secret:
        return False
    digest = hmac.new(secret.encode(), raw_body, hashlib.sha256).digest()
    return hmac.compare_digest(base64.b64encode(digest).decode(), header_hmac)


@dataclass(frozen=True)
class CheckoutSnapshot:
    token: str
    checkout_id: int | None
    phone: str | None  # E.164
    first_name: str | None
    currency: str
    total_price: Decimal
    total_price_usd: Decimal
    line_items: list[dict[str, Any]]
    recovery_url: str | None
    consent: bool
    consent_source: str | None
    updated_at: datetime
    completed_at: datetime | None


def _consent(p: dict[str, Any]) -> tuple[bool, str | None]:
    """Explicit opt-in only: a dedicated call-consent cart attribute first, then SMS consent."""
    attrs = {a.get("name"): a.get("value") for a in p.get("note_attributes") or []}
    if str(attrs.get("call_consent", "")).lower() in {"yes", "true", "1"}:
        return True, "cart_attribute"
    if p.get("buyer_accepts_sms_marketing"):
        return True, "sms_marketing"
    sms = ((p.get("customer") or {}).get("sms_marketing_consent") or {}).get("state")
    if sms == "subscribed":
        return True, "customer_sms_consent"
    return False, None


def parse_checkout(p: dict[str, Any]) -> CheckoutSnapshot:
    ship = p.get("shipping_address") or {}
    bill = p.get("billing_address") or {}
    customer = p.get("customer") or {}
    region = ship.get("country_code") or bill.get("country_code")
    raw_phone = (
        p.get("sms_marketing_phone")
        or p.get("phone")
        or ship.get("phone")
        or bill.get("phone")
        or customer.get("phone")
    )
    currency = p.get("presentment_currency") or p.get("currency") or "USD"
    total = Decimal(str(p.get("total_price") or "0"))
    consent, source = _consent(p)
    return CheckoutSnapshot(
        token=p["token"],
        checkout_id=p.get("id"),
        phone=normalize_phone(raw_phone, region),
        first_name=customer.get("first_name") or ship.get("first_name") or bill.get("first_name"),
        currency=currency,
        total_price=total,
        total_price_usd=(total * USD_RATES.get(currency, Decimal("0"))).quantize(
            Decimal("0.01"), ROUND_HALF_UP
        ),
        line_items=[
            {
                "title": li.get("title"),
                "variant_title": li.get("variant_title"),
                "variant_id": li.get("variant_id"),
                "quantity": li.get("quantity"),
                "price": li.get("price"),
            }
            for li in p.get("line_items") or []
        ],
        recovery_url=p.get("abandoned_checkout_url"),
        consent=consent,
        consent_source=source,
        updated_at=datetime.fromisoformat(p["updated_at"]),
        completed_at=datetime.fromisoformat(p["completed_at"]) if p.get("completed_at") else None,
    )
