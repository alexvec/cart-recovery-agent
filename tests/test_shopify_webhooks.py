import base64
import hashlib
import hmac
from decimal import Decimal

from recovery.shopify.webhooks import parse_checkout, verify_hmac

SECRET = "shpss_test"


def sign(body: bytes) -> str:
    return base64.b64encode(hmac.new(SECRET.encode(), body, hashlib.sha256).digest()).decode()


def test_hmac_accepts_valid_and_rejects_tampered() -> None:
    body = b'{"id": 1}'
    assert verify_hmac(body, sign(body), SECRET)
    assert not verify_hmac(b'{"id": 2}', sign(body), SECRET)
    assert not verify_hmac(body, None, SECRET)
    assert not verify_hmac(body, sign(body), "")


CHECKOUT = {
    "id": 123,
    "token": "tok123",
    "updated_at": "2026-10-21T12:00:00+04:00",
    "completed_at": None,
    "presentment_currency": "AED",
    "total_price": "550.00",
    "abandoned_checkout_url": "https://shop.example/recover/tok123",
    "buyer_accepts_sms_marketing": True,
    "shipping_address": {"country_code": "AE", "phone": "050 123 4567", "first_name": "Sara"},
    "line_items": [{"title": "Oud Intense 100ml", "quantity": 1, "price": "550.00", "variant_id": 9}],
}


def test_parse_checkout_normalizes_phone_currency_and_consent() -> None:
    snap = parse_checkout(CHECKOUT)
    assert snap.phone == "+971501234567"
    assert snap.total_price_usd == Decimal("149.77")
    assert (snap.consent, snap.consent_source) == (True, "sms_marketing")
    assert snap.first_name == "Sara"


def test_no_explicit_opt_in_means_no_consent() -> None:
    snap = parse_checkout({**CHECKOUT, "buyer_accepts_sms_marketing": False})
    assert (snap.consent, snap.consent_source) == (False, None)


def test_call_consent_cart_attribute_wins() -> None:
    snap = parse_checkout(
        {
            **CHECKOUT,
            "buyer_accepts_sms_marketing": False,
            "note_attributes": [{"name": "call_consent", "value": "yes"}],
        }
    )
    assert snap.consent_source == "cart_attribute"


def test_invalid_phone_is_dropped() -> None:
    assert parse_checkout({**CHECKOUT, "shipping_address": {"phone": "123"}}).phone is None
