"""
One-time script to register the product-created webhook with Shopify.

Usage:
    python -m agent.register_webhook
"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests
from config import SHOPIFY_STORE_URL, SHOPIFY_CLIENT_ID, SHOPIFY_CLIENT_SECRET, SHOPIFY_API_VERSION

SERVER_URL = "https://shopify-ai-storefront.onrender.com"
STORE_URL  = f"https://{SHOPIFY_STORE_URL}"
API_BASE   = f"{STORE_URL}/admin/api/{SHOPIFY_API_VERSION}"

def get_token():
    r = requests.post(f"{STORE_URL}/admin/oauth/access_token", data={
        "client_id": SHOPIFY_CLIENT_ID,
        "client_secret": SHOPIFY_CLIENT_SECRET,
        "grant_type": "client_credentials",
    }, timeout=10)
    r.raise_for_status()
    return r.json()["access_token"]

def main():
    token   = get_token()
    headers = {"X-Shopify-Access-Token": token, "Content-Type": "application/json"}

    # List existing webhooks
    r = requests.get(f"{API_BASE}/webhooks.json", headers=headers)
    existing = r.json().get("webhooks", [])
    print(f"Existing webhooks ({len(existing)}):")
    for w in existing:
        print(f"  id={w['id']}  topic={w['topic']}  address={w['address']}")

    # Remove stale product/create webhooks
    for w in existing:
        if w["topic"] == "products/create":
            requests.delete(f"{API_BASE}/webhooks/{w['id']}.json", headers=headers)
            print(f"  → Removed stale webhook id={w['id']}")

    # Register new webhook
    r = requests.post(f"{API_BASE}/webhooks.json", headers=headers, json={
        "webhook": {
            "topic":   "products/create",
            "address": f"{SERVER_URL}/webhooks/product-created",
            "format":  "json",
        }
    })
    if r.ok:
        w = r.json()["webhook"]
        print(f"\n✅ Webhook registered!")
        print(f"   id      : {w['id']}")
        print(f"   topic   : {w['topic']}")
        print(f"   address : {w['address']}")
    else:
        print(f"\n❌ Failed: {r.status_code} {r.text}")

if __name__ == "__main__":
    main()
