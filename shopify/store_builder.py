"""
Shopify Store Builder — creates collections, pages, and menus via Admin REST API.

Prerequisites:
  1. Create a Shopify Partner account and development store
  2. Create a Custom App in your store's Admin (Apps → Develop apps)
  3. Grant scopes: write_products, write_content, write_themes, write_navigation
  4. Install the app and copy the access token to config.py
"""

import json
import logging
import time
from typing import Any, Optional

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


# ── Low-level API client ──────────────────────────────────────────────────────

class ShopifyClient:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update(HEADERS)

    def get(self, endpoint: str, params: dict = None) -> dict:
        return self._request("GET", endpoint, params=params)

    def post(self, endpoint: str, body: dict) -> dict:
        return self._request("POST", endpoint, json=body)

    def put(self, endpoint: str, body: dict) -> dict:
        return self._request("PUT", endpoint, json=body)

    def _request(self, method: str, endpoint: str, **kwargs) -> dict:
        url = f"{API_BASE}{endpoint}"
        resp = self.session.request(method, url, **kwargs)

        # Respect Shopify rate limiting (leaky bucket: 40 req/s)
        if resp.status_code == 429:
            retry_after = float(resp.headers.get("Retry-After", 2))
            log.warning("Rate limited — sleeping %.1fs", retry_after)
            time.sleep(retry_after)
            return self._request(method, endpoint, **kwargs)

        resp.raise_for_status()
        return resp.json()


# ── Higher-level store builder ────────────────────────────────────────────────

class StoreBuilder:
    def __init__(self):
        self.client = ShopifyClient()

    # ── Collections ───────────────────────────────────────────────────────────

    def create_collection(self, title: str, description: str = "") -> dict:
        """Create a smart collection."""
        body = {
            "custom_collection": {
                "title": title,
                "body_html": f"<p>{description}</p>",
                "published": True,
            }
        }
        result = self.client.post("/custom_collections.json", body)
        coll = result.get("custom_collection", {})
        log.info("Created collection: %s (id=%s)", coll.get("title"), coll.get("id"))
        return coll

    def create_collections_from_nav(self, nav_items: list[str]) -> list[dict]:
        """
        Use navigation labels as collection names.
        Filters out generic items like Home, About, FAQ, etc.
        """
        skip = {"home", "about", "faq", "blog", "contact", "search", "cart", "account"}
        collections = []
        for label in nav_items:
            if label.lower() in skip:
                continue
            coll = self.create_collection(title=label)
            collections.append(coll)
            time.sleep(0.5)
        return collections

    # ── Pages ─────────────────────────────────────────────────────────────────

    def create_page(self, title: str, body_html: str) -> dict:
        body = {"page": {"title": title, "body_html": body_html, "published": True}}
        result = self.client.post("/pages.json", body)
        page = result.get("page", {})
        log.info("Created page: %s (id=%s)", page.get("title"), page.get("id"))
        return page

    def create_standard_pages(self) -> list[dict]:
        """Create About, FAQ, Contact, and Sustainability pages."""
        pages_data = [
            ("About Us", "<h1>About Us</h1><p>Tell your brand story here.</p>"),
            ("FAQ", "<h1>Frequently Asked Questions</h1><p>Add your FAQs here.</p>"),
            ("Contact", "<h1>Contact Us</h1><p>Reach out to us at hello@yourbrand.com</p>"),
            ("Sustainability", "<h1>Our Commitment</h1><p>Describe your values here.</p>"),
        ]
        return [self.create_page(t, h) for t, h in pages_data]

    # ── Navigation menus ──────────────────────────────────────────────────────

    def create_menu(self, title: str, handle: str, items: list[dict]) -> dict:
        """
        items: list of {"title": ..., "type": "collection_link"|"page_link"|"http_link",
                         "url": ...}
        """
        body = {
            "menu": {
                "title": title,
                "handle": handle,
                "items": items,
            }
        }
        result = self.client.post("/menus.json", body)
        menu = result.get("menu", {})
        log.info("Created menu: %s (id=%s)", menu.get("title"), menu.get("id"))
        return menu

    def build_main_menu(self, collections: list[dict]) -> dict:
        # /menus.json is not available in API v2026-04.
        # Navigation menus must be managed via the Shopify Admin UI:
        # Online Store → Navigation → Main menu
        log.info("Skipping menu API (not available in 2026-04). "
                 "Add navigation manually: Online Store → Navigation → Main menu")
        return {}

    # ── Store metadata ─────────────────────────────────────────────────────────

    def get_shop_info(self) -> dict:
        return self.client.get("/shop.json").get("shop", {})

    def print_store_summary(self) -> None:
        info = self.get_shop_info()
        print(f"\n{'='*50}")
        print(f"Store: {info.get('name')}")
        print(f"Domain: {info.get('domain')}")
        print(f"Email: {info.get('email')}")
        print(f"Plan: {info.get('plan_display_name')}")
        print(f"{'='*50}\n")
