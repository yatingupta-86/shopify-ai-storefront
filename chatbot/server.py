"""
AI Chatbot backend — FastAPI + Groq API (free tier, llama-3.3-70b, streaming)

Endpoints:
  POST /chat          → streaming chat response (SSE)
  GET  /widget.js     → serves the floating chat widget
  GET  /health        → health check

Run:
  export GROQ_API_KEY="gsk_xxxxxxxxxxxx"
  uvicorn chatbot.server:app --reload --port 8000
"""

import os
import re
import json
import time
from pathlib import Path

import requests as http_requests
from groq import Groq
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, FileResponse
from pydantic import BaseModel
from dotenv import load_dotenv

load_dotenv()

# ── Config ────────────────────────────────────────────────────────────────────
GROQ_API_KEY         = os.environ.get("GROQ_API_KEY", "")
GROQ_MODEL           = "llama-3.3-70b-versatile"
SHOPIFY_STORE_URL    = "https://myaistore-5.myshopify.com"
SHOPIFY_ACCESS_TOKEN = os.environ.get("SHOPIFY_ACCESS_TOKEN", "")
SHOPIFY_API_VERSION  = os.environ.get("SHOPIFY_API_VERSION", "2026-04")
SHOPIFY_API_BASE     = f"{SHOPIFY_STORE_URL}/admin/api/{SHOPIFY_API_VERSION}"
SHOPIFY_HEADERS      = {"X-Shopify-Access-Token": SHOPIFY_ACCESS_TOKEN}
WIDGET_JS_PATH       = Path(__file__).parent / "widget.js"

# Regex to detect order numbers like #1001, order 1001, order no 1001
ORDER_PATTERN = re.compile(r"(?:order[^\d]*|#)(\d{3,6})", re.IGNORECASE)

SYSTEM_PROMPT = """You are a friendly and helpful AI shopping assistant for myaistore —
an online footwear store based in India. You help customers with:

- Product recommendations based on their needs (running, casual, gym, etc.)
- Size guidance (we use UK sizing: UK 6–11)
- Pricing information (all prices are in Indian Rupees ₹)
- Order status and tracking (when order info is provided to you)
- Adding products to the customer's cart

Store URL: https://myaistore-5.myshopify.com

The live product catalogue (with variant IDs per size) will be in [PRODUCT CATALOGUE].
When order information is provided to you in [ORDER DATA], use it accurately.

## Adding to Cart
When a customer asks to add a product to their cart:
1. Identify the product and size from [PRODUCT CATALOGUE].
2. If size is not mentioned, ask for it before adding.
3. Once you have product + size, confirm what you're adding, then end your response
   with this exact token on its own line: [ADD_TO_CART:VARIANT_ID:QUANTITY]
   Example: [ADD_TO_CART:51490148745506:1]
4. Never make up a variant ID — only use IDs from [PRODUCT CATALOGUE].

Always be warm and helpful. Keep responses concise.
Always respond in the same language the customer uses (English or Hindi)."""

# ── Product catalogue cache (refreshes every 5 minutes) ──────────────────────
_product_cache: dict = {"data": None, "fetched_at": 0.0}
PRODUCT_CACHE_TTL = 300  # seconds


# ── Shopify product catalogue ─────────────────────────────────────────────────

def fetch_products() -> list[dict]:
    """Fetch all active products from Shopify, with 5-minute in-memory cache."""
    now = time.time()
    if _product_cache["data"] is not None and now - _product_cache["fetched_at"] < PRODUCT_CACHE_TTL:
        return _product_cache["data"]
    try:
        r = http_requests.get(
            f"{SHOPIFY_API_BASE}/products.json",
            headers=SHOPIFY_HEADERS,
            params={"status": "active", "limit": 250, "fields": "id,title,product_type,tags,variants,options,handle"},
            timeout=10,
        )
        r.raise_for_status()
        products = r.json().get("products", [])
        _product_cache["data"] = products
        _product_cache["fetched_at"] = now
        return products
    except Exception:
        return _product_cache["data"] or []


def format_products_context(products: list[dict]) -> str:
    """Convert Shopify product list into a readable catalogue for Groq.
    Includes variant IDs per size so the AI can trigger cart additions.
    """
    if not products:
        return "No products currently available."
    lines = []
    for i, p in enumerate(products, 1):
        variants = p.get("variants", [])

        # Price range
        prices = [float(v["price"]) for v in variants if v.get("price")]
        if prices:
            lo, hi = min(prices), max(prices)
            price_str = f"₹{lo:,.0f}" if lo == hi else f"₹{lo:,.0f}–₹{hi:,.0f}"
        else:
            price_str = "Price on request"

        # Tags and URL
        tags = p.get("tags", "")
        handle = p.get("handle", "")
        url = f"https://myaistore-5.myshopify.com/products/{handle}" if handle else ""

        header = f"{i}. {p['title']} — {price_str}"
        if tags:
            header += f" | Tags: {tags}"
        if url:
            header += f" | Link: {url}"

        # Variants with IDs (size → variant_id)
        variant_lines = []
        for v in variants:
            size = v.get("option1") or v.get("title", "")
            variant_lines.append(f"    {size}: variant_id={v['id']}")

        lines.append(header + "\n  Variants (size → variant_id):\n" + "\n".join(variant_lines))

    return "\n\n".join(lines)


# ── Shopify order lookup ──────────────────────────────────────────────────────

def fetch_order(order_number: str) -> dict | None:
    """Fetch order from Shopify by order number (e.g. '1001' or '#1001')."""
    name = f"#{order_number.lstrip('#')}"
    try:
        r = http_requests.get(
            f"{SHOPIFY_API_BASE}/orders.json",
            headers=SHOPIFY_HEADERS,
            params={"name": name, "status": "any"},
            timeout=10,
        )
        r.raise_for_status()
        orders = r.json().get("orders", [])
        return orders[0] if orders else None
    except Exception:
        return None


def format_order_context(order: dict) -> str:
    """Convert Shopify order dict into a readable context string for Groq."""
    lines = [
        f"Order Number  : {order.get('name')}",
        f"Date          : {order.get('created_at', '')[:10]}",
        f"Payment       : {order.get('financial_status', 'N/A').replace('_', ' ').title()}",
        f"Fulfillment   : {(order.get('fulfillment_status') or 'pending').replace('_', ' ').title()}",
        f"Total         : ₹{order.get('total_price', '0')}",
    ]

    # Line items
    items = order.get("line_items", [])
    if items:
        lines.append("Items:")
        for item in items:
            lines.append(f"  - {item['name']} x{item['quantity']} (₹{item['price']})")

    # Tracking
    fulfillments = order.get("fulfillments", [])
    if fulfillments:
        f = fulfillments[0]
        tracking_num = f.get("tracking_number", "N/A")
        tracking_url = f.get("tracking_url", "")
        lines.append(f"Tracking No   : {tracking_num}")
        if tracking_url:
            lines.append(f"Tracking URL  : {tracking_url}")

    # Shipping address
    addr = order.get("shipping_address")
    if addr:
        lines.append(f"Shipping To   : {addr.get('city', '')}, {addr.get('province', '')}")

    return "\n".join(lines)


# ── App ───────────────────────────────────────────────────────────────────────
app = FastAPI(title="myaistore Chatbot")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["POST", "GET", "OPTIONS"],
    allow_headers=["*"],
)

groq_client = Groq(api_key=GROQ_API_KEY)


# ── Request / Response models ─────────────────────────────────────────────────
class Message(BaseModel):
    role: str
    content: str

class ChatRequest(BaseModel):
    messages: list[Message]


# ── Chat endpoint (streaming SSE) ─────────────────────────────────────────────
@app.post("/chat")
async def chat(request: ChatRequest):
    """Stream Groq's response as Server-Sent Events.
    Automatically detects order numbers and injects live Shopify order data.
    """
    # Check latest user message for an order number
    last_user_msg = next(
        (m.content for m in reversed(request.messages) if m.role == "user"), ""
    )
    order_match = ORDER_PATTERN.search(last_user_msg)

    # Build messages for Groq — inject live product catalogue
    products = fetch_products()
    system = SYSTEM_PROMPT + f"\n\n[PRODUCT CATALOGUE]\n{format_products_context(products)}"

    if order_match:
        order_number = order_match.group(1)
        order = fetch_order(order_number)
        if order:
            order_context = format_order_context(order)
            system += f"\n\n[ORDER DATA]\n{order_context}"
        else:
            system += (
                f"\n\n[ORDER DATA]\nNo order found with number #{order_number}. "
                "Politely inform the customer and ask them to double-check the number."
            )

    messages = [{"role": "system", "content": system}]
    messages += [{"role": m.role, "content": m.content} for m in request.messages]

    def generate():
        stream = groq_client.chat.completions.create(
            model=GROQ_MODEL,
            messages=messages,
            max_tokens=1024,
            stream=True,
        )
        for chunk in stream:
            text = chunk.choices[0].delta.content
            if text:
                yield f"data: {json.dumps({'text': text})}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


# ── Serve widget JS ───────────────────────────────────────────────────────────
@app.get("/widget.js")
async def serve_widget():
    return FileResponse(WIDGET_JS_PATH, media_type="application/javascript")


# ── Health check ──────────────────────────────────────────────────────────────
@app.get("/health")
async def health():
    return {"status": "ok", "store": SHOPIFY_STORE_URL}
