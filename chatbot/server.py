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

import sys
import os as _os
_root = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
if _root not in sys.path:
    sys.path.insert(0, _root)

from observability import get_logger, init_sentry
init_sentry()
log = get_logger("server")

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
        log.error("shopify.token_fetch_failed", extra={"error": str(e)})
        return static_token  # fallback

def shopify_headers() -> dict:
    return {"X-Shopify-Access-Token": get_shopify_token()}

# Regex to detect order numbers like #1001, order 1001, order no 1001
ORDER_PATTERN = re.compile(r"(?:order[^\d]*|#)(\d{3,6})", re.IGNORECASE)

SYSTEM_PROMPT = """You are a friendly and helpful AI shopping assistant for Mera Shelf —
an online store selling handmade crochet gifts, toys, and accessories based in India. You help customers with:

- Product recommendations based on their needs (gifting, self-care, home decor, etc.)
- Information about materials, craftsmanship, and care instructions
- Pricing information (all prices are in Indian Rupees ₹)
- Order status and tracking (when order info is provided to you)
- Adding products to the customer's cart

Store URL: https://merashelf.com

The live product catalogue (with variant IDs) will be in [PRODUCT CATALOGUE].
When order information is provided to you in [ORDER DATA], use it accurately.

## Adding to Cart
When a customer asks to add a product to their cart:
1. Identify the product and variant from [PRODUCT CATALOGUE].
2. If the variant is unclear, ask for clarification before adding.
3. Once you have product + variant, confirm what you're adding, then end your response
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

# ── Cost ledger (in-memory) ───────────────────────────────────────────────────
# List of per-enrichment cost records
cost_ledger: list = []


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

    try:
        from agent.agent import run_product_agent, evaluate_confidence, fetch_price_history
    except Exception as e:
        log.error("agent.import_failed", extra={"error": str(e)})
        traceback.print_exc()
        return

    product_id = product.get("id")
    title = product.get("title", "Untitled")
    t0 = time.time()
    log.info("agent.enrichment_started", extra={"product_id": product_id, "title": title})

    try:
        enrichment, usage = run_product_agent(product, SHOPIFY_API_BASE, shopify_headers)
        if not enrichment:
            log.warning("agent.no_enrichment", extra={"product_id": product_id})
            return

        price_history = fetch_price_history(SHOPIFY_API_BASE, shopify_headers())
        auto_publish, reasons = evaluate_confidence(enrichment, price_history)

        updates = {
            "product": {
                "id": product_id,
                "title": enrichment.get("title", title),
                "body_html": enrichment["description"],
                "tags": ", ".join(enrichment.get("tags", [])),
                "metafields_global_title_tag": enrichment.get("seo_title", ""),
                "metafields_global_description_tag": enrichment.get("seo_description", ""),
            }
        }
        variants = product.get("variants", [])
        if variants and (not variants[0].get("price") or float(variants[0].get("price", 0)) == 0):
            updates["product"]["variants"] = [
                {"id": variants[0]["id"], "price": str(enrichment["suggested_price"])}
            ]

        # Set alt text on the first product image
        images = product.get("images", [])
        if images and enrichment.get("image_alt_text"):
            updates["product"]["images"] = [
                {"id": images[0]["id"], "alt": enrichment["image_alt_text"]}
            ]

        if auto_publish:
            updates["product"]["status"] = "active"
            r = http_requests.put(
                f"{SHOPIFY_API_BASE}/products/{product_id}.json",
                headers=shopify_headers(),
                json=updates,
                timeout=10,
            )
            if r.ok:
                log.info("agent.auto_published", extra={
                    "product_id": product_id, "title": title,
                    "duration_s": round(time.time() - t0, 1),
                })
            else:
                log.error("agent.publish_failed", extra={"product_id": product_id, "response": r.text[:200]})
        else:
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
            log.info("agent.queued_for_review", extra={
                "product_id": product_id, "review_id": review_id,
                "reasons": reasons, "duration_s": round(time.time() - t0, 1),
            })

        # Record to cost ledger
        cost_ledger.append({
            "ts":            time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "product_id":    product_id,
            "title":         title,
            "outcome":       "auto_published" if auto_publish else "queued_for_review",
            "duration_s":    round(time.time() - t0, 1),
            **usage,
        })

    except Exception as e:
        log.error("agent.enrichment_failed", extra={"product_id": product_id, "error": str(e)})
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
async def approve_review(review_id: str, request: Request):
    edits = None
    try:
        edits = await request.json()
    except Exception:
        pass
    """Seller approves a queued product, optionally with edits."""
    if review_id not in review_queue:
        raise HTTPException(status_code=404, detail="Review item not found")

    item = review_queue[review_id]
    updates = item["updates"]

    # Apply any seller edits
    if edits:
        if "title" in edits and edits["title"]:
            updates["product"]["title"] = edits["title"]
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

    # Assign to collection if selected
    if edits and edits.get("collection_id"):
        http_requests.post(
            f"{SHOPIFY_API_BASE}/collects.json",
            headers={**shopify_headers(), "Content-Type": "application/json"},
            json={"collect": {"product_id": item["product_id"], "collection_id": int(edits["collection_id"])}},
            timeout=10,
        )

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

    # Fetch live collections for dropdown
    from agent.agent import fetch_collections as _fetch_collections
    collections = _fetch_collections(SHOPIFY_API_BASE, shopify_headers())
    collection_options = "".join(
        f"<option value='{c['id']}'>{c['title']}</option>" for c in collections
    )

    inp = "input style='width:100%;padding:8px;border:1px solid #ddd;border-radius:6px;font-size:13px;box-sizing:border-box'"
    ta  = "textarea style='width:100%;padding:8px;border:1px solid #ddd;border-radius:6px;font-size:13px;box-sizing:border-box;resize:vertical'"

    cards = ""
    for item in pending:
        e = item["enrichment"]
        reasons_html = "".join(f"<li>⚠️ {r}</li>" for r in item["reasons"])
        tags_str = ", ".join(e.get("tags", []))
        desc = e.get("description", "").replace('"', "&quot;")
        price = e.get("suggested_price", 0)
        cards += f"""
        <div style="border:1px solid #ddd;border-radius:12px;padding:24px;margin:16px 0;font-family:Arial">
            <h2 style="margin:0 0 4px">{item['title']}</h2>
            <p style="color:#888;font-size:12px;margin:0 0 12px">Review ID: {item['review_id']} · {item['created_at']}</p>
            <h3 style="color:#d32f2f;margin:0 0 8px">Why Review is Needed</h3>
            <ul style="margin:0 0 16px;padding-left:20px">{reasons_html}</ul>
            <hr style="margin:16px 0">
            <h3 style="margin:0 0 12px">Edit &amp; Approve</h3>
            <label style="font-size:13px;font-weight:600">Title</label>
            <{inp} id="title-{item['review_id']}" value="{e.get('title', item['title']).replace(chr(34), '&quot;')}">
            <p style="font-size:11px;color:#888;margin:2px 0 10px">Original: <b>{item['title']}</b></p>
            <label style="font-size:13px;font-weight:600">Description</label>
            <{ta} id="desc-{item['review_id']}" rows="4">{e.get('description','')}</{ta.split()[0]}>
            <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-top:12px">
                <div>
                    <label style="font-size:13px;font-weight:600">Tags (comma separated)</label>
                    <{inp} id="tags-{item['review_id']}" value="{tags_str}">
                </div>
                <div>
                    <label style="font-size:13px;font-weight:600">Price (₹)</label>
                    <{inp} id="price-{item['review_id']}" type="number" value="{price}">
                </div>
            </div>
            <div style="margin-top:12px">
                <label style="font-size:13px;font-weight:600">Collection / Category</label>
                <select id="collection-{item['review_id']}"
                    style="width:100%;padding:8px;border:1px solid #ddd;border-radius:6px;font-size:13px;box-sizing:border-box">
                    <option value="">— No collection —</option>
                    {collection_options}
                </select>
                <p style="font-size:11px;color:#888;margin:4px 0 0">AI suggested: <b>{e.get('category','')}</b> (confidence: {e.get('category_confidence',0):.0%})</p>
            </div>
            <div style="display:flex;gap:12px;margin-top:16px">
                <button onclick="approve('{item['review_id']}')"
                    style="background:#2e7d32;color:#fff;border:none;padding:10px 24px;border-radius:8px;cursor:pointer;font-size:14px;font-weight:600">
                    ✅ Approve &amp; Publish
                </button>
                <button onclick="reject('{item['review_id']}')"
                    style="background:#c62828;color:#fff;border:none;padding:10px 24px;border-radius:8px;cursor:pointer;font-size:14px;font-weight:600">
                    ❌ Reject
                </button>
            </div>
        </div>"""

    return HTMLResponse(f"""<!DOCTYPE html>
<html><head><title>Product Review Queue</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
</head>
<body style="max-width:720px;margin:40px auto;padding:0 20px;font-family:Arial">
<h1>🔍 Product Review Queue <span style="font-size:16px;color:#888">({len(pending)} pending)</span></h1>
{cards}
<script>
const TOKEN = new URLSearchParams(location.search).get('token') || '';

async function approve(id) {{
    const title       = document.getElementById('title-'      + id).value;
    const desc        = document.getElementById('desc-'       + id).value;
    const tags        = document.getElementById('tags-'       + id).value;
    const price       = document.getElementById('price-'      + id).value;
    const collection  = document.getElementById('collection-' + id).value;
    const r = await fetch('/review-queue/' + id + '/approve', {{
        method: 'POST',
        headers: {{'Content-Type': 'application/json'}},
        body: JSON.stringify({{title: title, description: desc, tags: tags, price: parseFloat(price), collection_id: collection || null}})
    }});
    const d = await r.json();
    if (r.ok) {{ alert('✅ Product approved and published!'); location.reload(); }}
    else {{ alert('❌ Error: ' + (d.detail || 'Unknown error')); }}
}}

async function reject(id) {{
    if (!confirm('Reject this product? It will stay as draft in Shopify.')) return;
    const r = await fetch('/review-queue/' + id + '/reject', {{method: 'POST'}});
    if (r.ok) {{ alert('Product rejected.'); location.reload(); }}
}}
</script>
</body></html>""")


# ── Google Search Console verification ───────────────────────────────────────
@app.get("/googled4426b159fb66caa.html", response_class=HTMLResponse)
async def google_verification():
    return HTMLResponse("google-site-verification: googled4426b159fb66caa.html")


# ── Agent cost dashboard ──────────────────────────────────────────────────────

@app.get("/costs", response_class=HTMLResponse)
async def cost_dashboard(token: str = ""):
    if not ADMIN_TOKEN or token != ADMIN_TOKEN:
        return HTMLResponse("<h2 style='font-family:Arial;padding:40px'>401 Unauthorized</h2>", status_code=401)

    total_inr  = sum(r["cost_inr"] for r in cost_ledger)
    total_usd  = sum(r["cost_usd"] for r in cost_ledger)
    total_in   = sum(r["input_tokens"] for r in cost_ledger)
    total_out  = sum(r["output_tokens"] for r in cost_ledger)
    total_runs = len(cost_ledger)
    auto_count = sum(1 for r in cost_ledger if r["outcome"] == "auto_published")
    review_count = total_runs - auto_count
    avg_cost   = round(total_inr / total_runs, 2) if total_runs else 0
    avg_dur    = round(sum(r["duration_s"] for r in cost_ledger) / total_runs, 1) if total_runs else 0

    rows = ""
    for r in reversed(cost_ledger):
        outcome_color = "#2e7d32" if r["outcome"] == "auto_published" else "#e65100"
        outcome_label = "✅ Auto-published" if r["outcome"] == "auto_published" else "⚠️ Queued"
        rows += f"""
        <tr>
          <td>{r['ts']}</td>
          <td style="max-width:220px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap"
              title="{r['title']}">{r['title']}</td>
          <td style="color:{outcome_color};font-weight:600">{outcome_label}</td>
          <td>{r['claude_calls']}</td>
          <td>{', '.join(r['tool_calls'])}</td>
          <td>{r['input_tokens']:,}</td>
          <td>{r['output_tokens']:,}</td>
          <td>{r['duration_s']}s</td>
          <td style="font-weight:600">₹{r['cost_inr']}</td>
          <td>${r['cost_usd']}</td>
        </tr>"""

    if not rows:
        rows = "<tr><td colspan='10' style='text-align:center;color:#888;padding:32px'>No enrichments run yet.</td></tr>"

    return HTMLResponse(f"""<!DOCTYPE html>
<html><head>
<title>Agent Cost Dashboard</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
  body {{ font-family: Arial, sans-serif; background: #fdf6ec; margin: 0; padding: 24px; color: #3b2007; }}
  h1 {{ margin: 0 0 24px; font-size: 24px; }}
  .cards {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 16px; margin-bottom: 32px; }}
  .card {{ background: #fff; border-radius: 10px; padding: 18px 20px; border-top: 4px solid #97124f; }}
  .card .val {{ font-size: 28px; font-weight: 700; color: #97124f; }}
  .card .lbl {{ font-size: 12px; color: #888; margin-top: 4px; }}
  table {{ width: 100%; border-collapse: collapse; background: #fff; border-radius: 10px; overflow: hidden; font-size: 13px; }}
  th {{ background: #3b2007; color: #fff; padding: 10px 12px; text-align: left; font-size: 12px; }}
  td {{ padding: 10px 12px; border-bottom: 1px solid #f0e8d8; }}
  tr:last-child td {{ border-bottom: none; }}
  tr:hover td {{ background: #fdf6ec; }}
</style>
</head>
<body>
<h1>🤖 Agent Cost Dashboard</h1>
<div class="cards">
  <div class="card"><div class="val">₹{total_inr:.2f}</div><div class="lbl">Total spend (INR)</div></div>
  <div class="card"><div class="val">${total_usd:.4f}</div><div class="lbl">Total spend (USD)</div></div>
  <div class="card"><div class="val">{total_runs}</div><div class="lbl">Total enrichments</div></div>
  <div class="card"><div class="val">₹{avg_cost}</div><div class="lbl">Avg cost / product</div></div>
  <div class="card"><div class="val">{avg_dur}s</div><div class="lbl">Avg duration</div></div>
  <div class="card"><div class="val">{auto_count}</div><div class="lbl">Auto-published</div></div>
  <div class="card"><div class="val">{review_count}</div><div class="lbl">Sent to review</div></div>
  <div class="card"><div class="val">{total_in + total_out:,}</div><div class="lbl">Total tokens used</div></div>
</div>
<table>
  <thead><tr>
    <th>Time</th><th>Product</th><th>Outcome</th><th>Claude Calls</th>
    <th>Tools Called</th><th>Input Tokens</th><th>Output Tokens</th>
    <th>Duration</th><th>Cost (₹)</th><th>Cost ($)</th>
  </tr></thead>
  <tbody>{rows}</tbody>
</table>
<p style="color:#aaa;font-size:11px;margin-top:16px">
  Pricing: Claude Opus 4.6 — $15/1M input tokens, $75/1M output tokens · ₹84 per USD · Resets on server restart
</p>
</body></html>""")


# ── Health check ──────────────────────────────────────────────────────────────
@app.get("/health")
async def health():
    return {"status": "ok", "store": SHOPIFY_STORE_URL}
