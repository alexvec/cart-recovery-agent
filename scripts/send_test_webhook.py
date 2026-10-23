"""Send a signed, realistic Shopify webhook to the local API (no Shopify needed).

uv run python scripts/send_test_webhook.py checkout --token demo1 --phone "+971501234567"
uv run python scripts/send_test_webhook.py order --token demo1
"""

import argparse
import base64
import hashlib
import hmac
import json
import uuid
from datetime import UTC, datetime

import httpx

from recovery.config import get_settings

SHOP = "cart-recovery-demo-cvndysvd.myshopify.com"


def checkout_payload(token: str, phone: str, total: str) -> dict[str, object]:
    return {
        "id": abs(hash(token)) % 10**12,
        "token": token,
        "updated_at": datetime.now(UTC).isoformat(),
        "completed_at": None,
        "presentment_currency": "AED",
        "total_price": total,
        "abandoned_checkout_url": f"https://{SHOP}/checkouts/{token}/recover",
        "buyer_accepts_sms_marketing": True,
        "shipping_address": {"country_code": "AE", "phone": phone, "first_name": "Sara"},
        "line_items": [{"title": "Oud Intense Eau de Parfum 100ml", "quantity": 1, "price": total}],
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("kind", choices=["checkout", "order"])
    ap.add_argument("--token", required=True)
    ap.add_argument("--phone", default="+971501234567")
    ap.add_argument("--total", default="650.00")
    ap.add_argument("--url", default="http://localhost:8090/webhooks/shopify")
    a = ap.parse_args()

    if a.kind == "checkout":
        topic, payload = "checkouts/update", checkout_payload(a.token, a.phone, a.total)
    else:
        topic = "orders/create"
        payload = {"id": int(uuid.uuid4().int % 10**12), "checkout_token": a.token, "total_price": a.total}

    body = json.dumps(payload).encode()
    secret = get_settings().shopify_api_secret.get_secret_value().encode()
    signature = base64.b64encode(hmac.new(secret, body, hashlib.sha256).digest()).decode()
    res = httpx.post(
        a.url,
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-Shopify-Hmac-Sha256": signature,
            "X-Shopify-Topic": topic,
            "X-Shopify-Shop-Domain": SHOP,
            "X-Shopify-Webhook-Id": str(uuid.uuid4()),
        },
    )
    print(res.status_code, res.text)


if __name__ == "__main__":
    main()
