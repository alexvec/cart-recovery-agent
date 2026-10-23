"""Seed the dev store with a small AED perfume catalog (prices chosen around the break-even threshold).

uv run python scripts/shopify/seed_products.py --store cart-recovery-demo-cvndysvd.myshopify.com
"""

import argparse
import json
import subprocess
from pathlib import Path

MUTATION = Path(__file__).with_name("product_set.graphql")

# AED prices. UAE break-even is ~$148 (~AED 545): some carts land above it, some below.
PRODUCTS = [
    ("Oud Intense Eau de Parfum 100ml", "Oud & Musk", "650.00"),
    ("Amber Nights Eau de Parfum 50ml", "Oud & Musk", "420.00"),
    ("Rose Taifi Extrait 30ml", "Oud & Musk", "890.00"),
    ("Bakhoor Gift Set", "Oud & Musk", "1250.00"),
    ("Musk Tahara Body Mist 200ml", "Oud & Musk", "120.00"),
    ("Discovery Set (6 x 2ml)", "Oud & Musk", "95.00"),
]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--store", required=True)
    store = ap.parse_args().store
    for title, vendor, price in PRODUCTS:
        variables = {
            "input": {
                "title": title,
                "vendor": vendor,
                "productType": "Fragrance",
                "status": "ACTIVE",
                "productOptions": [{"name": "Title", "values": [{"name": "Default Title"}]}],
                "variants": [
                    {
                        "optionValues": [{"optionName": "Title", "name": "Default Title"}],
                        "price": price,
                        "inventoryPolicy": "CONTINUE",
                    }
                ],
            }
        }
        out = subprocess.run(
            [
                "shopify",
                "store",
                "execute",
                "--store",
                store,
                "--query-file",
                str(MUTATION),
                "--variables",
                json.dumps(variables),
                "--allow-mutations",
                "--json",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        print(title, "->", out.stdout.strip()[-300:] or out.stderr.strip()[-300:])


if __name__ == "__main__":
    main()
