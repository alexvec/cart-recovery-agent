"""Dynamic variables the agent's prompt and first message are rendered with."""

from recovery import pii
from recovery.db import Checkout, Shop


def call_variables(workflow_id: str, shop: Shop, checkout: Checkout) -> dict[str, str]:
    return {
        "workflow_id": workflow_id,
        "shop_name": shop.name or shop.domain.split(".")[0],
        "first_name": pii.decrypt(checkout.first_name_enc) if checkout.first_name_enc else "there",
        "cart_summary": ", ".join(f"{li['quantity']} x {li['title']}" for li in checkout.line_items),
        "cart_total": f"{checkout.total_price} {checkout.currency}",
    }
