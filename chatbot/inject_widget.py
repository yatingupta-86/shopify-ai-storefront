"""
Injects the AI chat widget into Shopify via the Script Tags API.

Usage:
    python -m chatbot.inject_widget --api-url https://abc123.ngrok-free.app

This registers a <script> tag on every storefront page that loads widget.js
from your running FastAPI server.
"""

import argparse
import sys
import os

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from config import SHOPIFY_STORE_URL, SHOPIFY_ACCESS_TOKEN, SHOPIFY_API_VERSION

BASE    = f"https://{SHOPIFY_STORE_URL}/admin/api/{SHOPIFY_API_VERSION}"
HEADERS = {"X-Shopify-Access-Token": SHOPIFY_ACCESS_TOKEN, "Content-Type": "application/json"}


def list_script_tags():
    r = requests.get(f"{BASE}/script_tags.json", headers=HEADERS, timeout=10)
    r.raise_for_status()
    return r.json().get("script_tags", [])


def remove_old_widget_tags():
    """Remove any previously injected chatbot script tags."""
    removed = 0
    for tag in list_script_tags():
        if "widget.js" in tag.get("src", ""):
            r = requests.delete(f"{BASE}/script_tags/{tag['id']}.json", headers=HEADERS, timeout=10)
            if r.ok:
                print(f"  Removed old tag id={tag['id']}")
                removed += 1
    return removed


def inject(api_url: str):
    api_url = api_url.rstrip("/")
    widget_url = f"{api_url}/widget.js"

    print(f"\nInjecting widget into {SHOPIFY_STORE_URL}...")
    print(f"  Widget URL: {widget_url}")

    # Patch the placeholder in widget.js with the real API URL
    widget_path = os.path.join(os.path.dirname(__file__), "widget.js")
    with open(widget_path, encoding="utf-8") as f:
        js = f.read()

    if "%%CHATBOT_API_URL%%" in js:
        patched = js.replace("%%CHATBOT_API_URL%%", api_url)
        patched_path = widget_path + ".patched"
        with open(patched_path, "w", encoding="utf-8") as f:
            f.write(patched)
        print(f"  Patched widget saved to {patched_path}")
        print(f"  ⚠  Copy {patched_path} → {widget_path} before starting the server,")
        print(f"      OR set CHATBOT_API_URL env variable and restart the server.")

    # Remove stale tags
    removed = remove_old_widget_tags()
    if removed:
        print(f"  Cleaned up {removed} old tag(s).")

    # Register new script tag
    body = {
        "script_tag": {
            "event": "onload",
            "src": widget_url,
            "display_scope": "online_store",
        }
    }
    r = requests.post(f"{BASE}/script_tags.json", headers=HEADERS, json=body, timeout=10)
    r.raise_for_status()
    tag = r.json()["script_tag"]

    print(f"\n✅ Widget injected successfully!")
    print(f"   Script tag id : {tag['id']}")
    print(f"   Widget URL    : {tag['src']}")
    print(f"   Scope         : {tag['display_scope']}")
    print(f"\nVisit {SHOPIFY_STORE_URL} to see the chat button (bottom-right corner).\n")
    return tag


def remove():
    removed = remove_old_widget_tags()
    if removed:
        print(f"✅ Removed {removed} widget script tag(s).")
    else:
        print("No widget script tags found.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Inject or remove AI chat widget from Shopify")
    sub = parser.add_subparsers(dest="command")

    inject_parser = sub.add_parser("inject", help="Inject widget script tag")
    inject_parser.add_argument("--api-url", required=True,
                               help="Public URL of your chatbot server (e.g. https://abc.ngrok-free.app)")

    sub.add_parser("remove", help="Remove widget script tag(s)")
    sub.add_parser("list",   help="List current script tags")

    args = parser.parse_args()

    if args.command == "inject":
        inject(args.api_url)
    elif args.command == "remove":
        remove()
    elif args.command == "list":
        tags = list_script_tags()
        print(f"Script tags ({len(tags)}):")
        for t in tags:
            print(f"  id={t['id']}  src={t['src']}")
    else:
        parser.print_help()
