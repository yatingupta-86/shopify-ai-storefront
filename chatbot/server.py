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
import hmac
import hashlib
import uuid
from pathlib import Path
from typing import Optional

import requests as http_requests
from groq import Groq
from fastapi import FastAPI, Request, BackgroundTasks, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, FileResponse, HTMLResponse
from pydantic import BaseModel
from dotenv import load_dotenv

load_dotenv()

# ── Config ────────────────────────────────────────────────────────────────────
GROQ_API_KEY          = os.environ.get("GROQ_API_KEY", "")
GROQ_MODEL            = "llama-3.3-70b-versatile"
SHOPIFY_STORE_DOMAIN  = os.environ.get("SHOPIFY_STORE_URL", "mera-shelf.myshopify.com")
SHOPIFY_STORE_URL     = f"https://{SHOPIFY_STORE_DOMAIN}"
SHOPIFY_CLIENT_ID     = os.environ.get("SHOPIFY_CLIENT_ID", "")
SHOPIFY_CLIENT_SECRET = os.environ.get("SHOPIFY_CLIENT_SECRET", "")
SHOPIFY_API_VERSION   = os.environ.get("SHOPIFY_API_VERSION", "2026-04")
SHOPIFY_API_BASE      = f"{SHOPIFY_STORE_URL}/admin/api/{SHOPIFY_API_VERSION}"
WIDGET_JS_PATH        = Path(__file__).parent / "widget.js"

# ── Client Credentials Token (Dev Dashboard apps) ────────────────────────────
_token_cache: dict = {"token": None, "expires_at": 0.0}

def get_shopify_token() -> str:
    """Fetch a short-lived access token using client credentials grant.
    Tokens are cached until 60s before expiry.
    Falls back to SHOPIFY_ACCESS_TOKEN env var if client credentials not set.
    """
    # Fall back to static token if no client credentials configured
    static_token = os.environ.get("SHOPIFY_ACCESS_TOKEN", "")
    if not SHOPIFY_CLIENT_ID or not SHOPIFY_CLIENT_SECRET:
        return static_token

    now = time.time()
    if _token_cache["token"] and now < _token_cache["expires_at"] - 60:
        return _token_cache["token"]

    try:
        r = http_requests.post(
            f"{SHOPIFY_STORE_URL}/admin/oauth/access_token",
            data={
                "client_id": SHOPIFY_CLIENT_ID,
                "client_secret": SHOPIFY_CLIENT_SECRET,
                "grant_type": "client_credentials",
            },
            timeout=10,
        )
        r.raise_for_status()
        data = r.json()
        _token_cache["token"] = data["access_token"]
        _token_cache["expires_at"] = now + data.get("expires_in", 3600)
        return _token_cache["token"]
    except Exception as e:
        print(f"[token] failed to fetch token: {e}")
        return static_token  # fallback

def shopify_headers() -> dict:
    return {"X-Shopify-Access-Token": get_shopify_token()}

# Regex to detect order numbers like #1001, order 1001, order no 1001
ORDER_PATTERN = re.compile(r"(?:order[^\d]*|#)(\d{3,6})", re.IGNORECASE)

SYSTEM_PROMPT = """You are a friendly and helpful AI shopping assistant for Mera Shelf —
an online footwear store based in India. You help customers with:

- Product recommendations based on their needs (running, casual, gym, etc.)
- Size guidance (we use UK sizing: UK 6–11)
- Pricing information (all prices are in Indian Rupees ₹)
- Order status and tracking (when order info is provided to you)
- Adding products to the customer's cart

Store URL: https://mera-shelf.myshopify.com

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
            headers=shopify_headers(),
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
        url = f"{SHOPIFY_STORE_URL}/products/{handle}" if handle else ""

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
            headers=shopify_headers(),
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
app = FastAPI(title="Mera Shelf Chatbot")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["POST", "GET", "OPTIONS"],
    allow_headers=["*"],
)

groq_client = Groq(api_key=GROQ_API_KEY)

# ── Review queue (in-memory) ──────────────────────────────────────────────────
# { review_id: { product_id, title, enrichment, reasons, status, created_at } }
review_queue: dict = {}


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


# ── Product enrichment agent (background task) ────────────────────────────────

def enrich_product_background(product: dict):
    """Runs agent, then auto-publishes or queues for review."""
    import traceback
    import sys
    import os

    # Ensure project root is on path so agent module is found
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if root not in sys.path:
        sys.path.insert(0, root)

    try:
        from agent.agent import run_product_agent, evaluate_confidence, fetch_price_history
    except Exception as e:
        print(f"[agent] ❌ Import error: {e}", flush=True)
        traceback.print_exc()
        return

    product_id = product.get("id")
    title = product.get("title", "Untitled")
    print(f"[agent] Starting enrichment for product '{title}' (id={product_id})", flush=True)

    try:
        enrichment = run_product_agent(product, SHOPIFY_API_BASE, shopify_headers)
        if not enrichment:
            print(f"[agent] No enrichment returned for product {product_id}", flush=True)
            return

        price_history = fetch_price_history(SHOPIFY_API_BASE, shopify_headers())
        auto_publish, reasons = evaluate_confidence(enrichment, price_history)

        updates = {
            "product": {
                "id": product_id,
                "body_html": enrichment["description"],
                "tags": ", ".join(enrichment.get("tags", [])),
            }
        }
        # Set price on first variant if not already set
        variants = product.get("variants", [])
        if variants and (not variants[0].get("price") or float(variants[0].get("price", 0)) == 0):
            updates["product"]["variants"] = [
                {"id": variants[0]["id"], "price": str(enrichment["suggested_price"])}
            ]

        if auto_publish:
            # Apply enrichment and publish directly
            updates["product"]["status"] = "active"
            r = http_requests.put(
                f"{SHOPIFY_API_BASE}/products/{product_id}.json",
                headers=shopify_headers(),
                json=updates,
                timeout=10,
            )
            if r.ok:
                print(f"[agent] ✅ Auto-published product '{title}' with enrichment")
            else:
                print(f"[agent] ❌ Failed to update product: {r.text}")
        else:
            # Queue for seller review — keep product as draft
            review_id = str(uuid.uuid4())[:8]
            review_queue[review_id] = {
                "review_id": review_id,
                "product_id": product_id,
                "title": title,
                "enrichment": enrichment,
                "reasons": reasons,
                "updates": updates,
                "status": "pending",
                "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }
            print(f"[agent] ⚠️  Queued for review (id={review_id}): {reasons}")

    except Exception as e:
        print(f"[agent] ❌ Error during enrichment: {e}")
        traceback.print_exc()


# ── Shopify webhook: product created ──────────────────────────────────────────

@app.post("/webhooks/product-created")
async def webhook_product_created(request: Request, background_tasks: BackgroundTasks):
    """Shopify calls this when a new product is created."""
    body = await request.body()

    # Verify HMAC signature
    shopify_secret = SHOPIFY_CLIENT_SECRET.encode("utf-8")
    hmac_header = request.headers.get("X-Shopify-Hmac-Sha256", "")
    computed = hmac.new(shopify_secret, body, hashlib.sha256).digest()
    import base64
    computed_b64 = base64.b64encode(computed).decode("utf-8")
    if not hmac.compare_digest(computed_b64, hmac_header):
        raise HTTPException(status_code=401, detail="Invalid webhook signature")

    product = json.loads(body)
    background_tasks.add_task(enrich_product_background, product)
    return {"status": "accepted"}


# ── Review queue endpoints ────────────────────────────────────────────────────

@app.get("/review-queue")
async def get_review_queue():
    """List all products pending seller review."""
    pending = [v for v in review_queue.values() if v["status"] == "pending"]
    return {"count": len(pending), "items": pending}


@app.post("/review-queue/{review_id}/approve")
async def approve_review(review_id: str, edits: Optional[dict] = None):
    """Seller approves a queued product, optionally with edits."""
    if review_id not in review_queue:
        raise HTTPException(status_code=404, detail="Review item not found")

    item = review_queue[review_id]
    updates = item["updates"]

    # Apply any seller edits
    if edits:
        if "description" in edits:
            updates["product"]["body_html"] = edits["description"]
        if "price" in edits:
            variants = updates["product"].get("variants", [])
            if variants:
                variants[0]["price"] = str(edits["price"])
        if "tags" in edits:
            updates["product"]["tags"] = edits["tags"]

    updates["product"]["status"] = "active"
    r = http_requests.put(
        f"{SHOPIFY_API_BASE}/products/{item['product_id']}.json",
        headers=shopify_headers(),
        json=updates,
        timeout=10,
    )
    if not r.ok:
        raise HTTPException(status_code=500, detail=f"Shopify update failed: {r.text}")

    review_queue[review_id]["status"] = "approved"
    return {"status": "approved", "product_id": item["product_id"]}


@app.post("/review-queue/{review_id}/reject")
async def reject_review(review_id: str):
    """Seller rejects — product stays as draft in Shopify."""
    if review_id not in review_queue:
        raise HTTPException(status_code=404, detail="Review item not found")
    review_queue[review_id]["status"] = "rejected"
    return {"status": "rejected"}


ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "")

@app.get("/review-queue/ui", response_class=HTMLResponse)
async def review_queue_ui(token: str = ""):
    """Simple HTML UI for seller to review queued products."""
    if not ADMIN_TOKEN or token != ADMIN_TOKEN:
        return HTMLResponse("<h2 style='font-family:Arial;padding:40px'>401 Unauthorized</h2>", status_code=401)
    pending = [v for v in review_queue.values() if v["status"] == "pending"]
    if not pending:
        return HTMLResponse("<h2 style='font-family:Arial;padding:40px'>✅ No products pending review.</h2>")

    cards = ""
    for item in pending:
        e = item["enrichment"]
        reasons_html = "".join(f"<li>⚠️ {r}</li>" for r in item["reasons"])
        tags_str = ", ".join(e.get("tags", []))
        cards += f"""
        <div style="border:1px solid #ddd;border-radius:12px;padding:24px;margin:16px 0;font-family:Arial">
            <h2 style="margin:0 0 8px">{item['title']}</h2>
            <p style="color:#888;font-size:13px">Review ID: {item['review_id']} · Created: {item['created_at']}</p>
            <hr>
            <h3>AI Generated Content</h3>
            <p><b>Description:</b> {e.get('description','')}</p>
            <p><b>Category:</b> {e.get('category','')} <span style="color:#888">(confidence: {e.get('category_confidence',0):.0%})</span></p>
            <p><b>Tags:</b> {tags_str}</p>
            <p><b>Suggested Price:</b> ₹{e.get('suggested_price',0):,.0f}</p>
            <p><b>Image Quality:</b> {e.get('image_quality','')}</p>
            <h3 style="color:#d32f2f">Why Review is Needed</h3>
            <ul>{reasons_html}</ul>
            <div style="display:flex;gap:12px;margin-top:16px">
                <button onclick="action('{item['review_id']}','approve')"
                    style="background:#2e7d32;color:#fff;border:none;padding:10px 24px;border-radius:8px;cursor:pointer;font-size:14px">
                    ✅ Approve &amp; Publish
                </button>
                <button onclick="action('{item['review_id']}','reject')"
                    style="background:#c62828;color:#fff;border:none;padding:10px 24px;border-radius:8px;cursor:pointer;font-size:14px">
                    ❌ Reject
                </button>
            </div>
        </div>"""

    return HTMLResponse(f"""<!DOCTYPE html>
<html><head><title>Product Review Queue</title></head>
<body style="max-width:700px;margin:40px auto;padding:0 20px">
<h1 style="font-family:Arial">🔍 Product Review Queue ({len(pending)} pending)</h1>
{cards}
<script>
async function action(id, type) {{
    const r = await fetch('/review-queue/' + id + '/' + type, {{method:'POST'}});
    const d = await r.json();
    alert(type === 'approve' ? '✅ Product approved and published!' : '❌ Product rejected.');
    location.reload();
}}
</script>
</body></html>""")


# ── Health check ──────────────────────────────────────────────────────────────
@app.get("/health")
async def health():
    return {"status": "ok", "store": SHOPIFY_STORE_URL}
