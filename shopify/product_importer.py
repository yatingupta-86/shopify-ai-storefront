"""
Product Importer — imports YOUR products into Shopify via Admin REST API.

Input: a JSON file with product data (you provide this — do not copy competitor data).
Format:
  [
    {
      "title": "Classic Runner",
      "vendor": "Your Brand",
      "product_type": "Footwear",
      "body_html": "<p>Product description here.</p>",
      "tags": "sustainable, running, natural",
      "variants": [
        {"option1": "US 8", "price": "95.00", "sku": "SKU-001-8", "inventory_quantity": 50},
        {"option1": "US 9", "price": "95.00", "sku": "SKU-001-9", "inventory_quantity": 50}
      ],
      "options": [{"name": "Size", "values": ["US 8", "US 9"]}],
      "images": [{"src": "https://your-cdn.com/product-image.jpg", "alt": "Classic Runner"}]
    }
  ]
"""

import json
import logging
import time
from pathlib import Path
from typing import Optional

import requests

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from config import SHOPIFY_STORE_URL, SHOPIFY_ACCESS_TOKEN, SHOPIFY_API_VERSION

log = logging.getLogger(__name__)

API_BASE = f"https://{SHOPIFY_STORE_URL}/admin/api/{SHOPIFY_API_VERSION}"
HEADERS = {
    "X-Shopify-Access-Token": SHOPIFY_ACCESS_TOKEN,
    "Content-Type": "application/json",
}


class ProductImporter:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update(HEADERS)
        self.imported: list[dict] = []
        self.failed: list[dict] = []

    # ── Public API ────────────────────────────────────────────────────────────

    def import_from_file(self, json_path: str) -> tuple[list, list]:
        """Import all products from a JSON file. Returns (imported, failed)."""
        products = json.loads(Path(json_path).read_text(encoding="utf-8"))
        log.info("Importing %d products from %s", len(products), json_path)
        for product in products:
            self._import_one(product)
            time.sleep(0.5)   # Stay within rate limits
        log.info("Done. Imported: %d  Failed: %d", len(self.imported), len(self.failed))
        return self.imported, self.failed

    def import_from_list(self, products: list[dict]) -> tuple[list, list]:
        """Import from an in-memory list of product dicts."""
        for product in products:
            self._import_one(product)
            time.sleep(0.5)
        return self.imported, self.failed

    # ── Internal ──────────────────────────────────────────────────────────────

    def _import_one(self, product: dict) -> Optional[dict]:
        body = {"product": {**product, "status": "draft"}}
        try:
            resp = self.session.post(f"{API_BASE}/products.json", json=body)
            if resp.status_code == 429:
                time.sleep(float(resp.headers.get("Retry-After", 2)))
                resp = self.session.post(f"{API_BASE}/products.json", json=body)
            resp.raise_for_status()
            created = resp.json()["product"]
            log.info("Imported: %s (id=%s)", created["title"], created["id"])
            self.imported.append(created)
            return created
        except requests.HTTPError as e:
            log.error("Failed to import '%s': %s", product.get("title"), e)
            self.failed.append({"product": product, "error": str(e)})
            return None

    # ── Assign to collection ──────────────────────────────────────────────────

    def add_product_to_collection(self, product_id: int, collection_id: int) -> None:
        body = {"collect": {"product_id": product_id, "collection_id": collection_id}}
        resp = self.session.post(f"{API_BASE}/collects.json", json=body)
        if resp.ok:
            log.debug("Added product %s to collection %s", product_id, collection_id)
        else:
            log.warning("Could not add product %s to collection: %s", product_id, resp.text[:200])


# ── Sample data generator (placeholder — replace with your real products) ─────

def generate_sample_products(n: int = 5) -> list[dict]:
    """
    Generate n placeholder products so you can test the pipeline end-to-end.
    Replace with your actual product catalog before going live.
    """
    return [
        {
            "title": f"Sample Product {i+1}",
            "vendor": "Your Brand",
            "product_type": "Footwear",
            "body_html": f"<p>High-quality product #{i+1}. Add your description here.</p>",
            "tags": "sample, placeholder",
            "variants": [
                {"option1": size, "price": "95.00", "sku": f"SAMPLE-{i+1}-{size.replace(' ', '')}",
                 "inventory_management": "shopify", "inventory_quantity": 20}
                for size in ["US 7", "US 8", "US 9", "US 10", "US 11"]
            ],
            "options": [{"name": "Size", "values": ["US 7", "US 8", "US 9", "US 10", "US 11"]}],
        }
        for i in range(n)
    ]


if __name__ == "__main__":
    importer = ProductImporter()
    samples = generate_sample_products(3)
    imported, failed = importer.import_from_list(samples)
    print(f"\nImported {len(imported)} products, {len(failed)} failed.")
