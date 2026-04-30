"""
AI Agent for automatic product enrichment.

When a seller uploads a product to Shopify:
  1. Fetches store context (collections, similar products, price history)
  2. Analyzes product image with Claude Vision
  3. Generates description, category, tags, price suggestion
  4. Applies confidence gates → auto-publish or queue for human review
"""

import base64
import json
import os
import sys

import anthropic
import requests as http_requests

# Ensure project root on path so observability is importable
_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _root not in sys.path:
    sys.path.insert(0, _root)

from observability import get_logger
log = get_logger("agent")


class _nullspan:
    """No-op context manager used when Sentry is not available."""
    def __enter__(self): return None
    def __exit__(self, *_): pass

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

# ── Confidence thresholds ─────────────────────────────────────────────────────
CATEGORY_CONFIDENCE_THRESHOLD = 0.85
PRICE_TOLERANCE = 0.20   # allow 20% outside historical range


# ── Tool implementations ──────────────────────────────────────────────────────

def fetch_collections(api_base: str, headers: dict) -> list[dict]:
    collections = []
    for kind in ("custom_collections", "smart_collections"):
        r = http_requests.get(f"{api_base}/{kind}.json", headers=headers, timeout=10)
        if r.ok:
            collections += r.json().get(kind, [])
    return [{"id": c["id"], "title": c["title"]} for c in collections]


def fetch_similar_products(api_base: str, headers: dict, tags: str) -> list[dict]:
    r = http_requests.get(
        f"{api_base}/products.json", headers=headers,
        params={"status": "active", "limit": 50, "fields": "title,tags,variants"},
        timeout=10,
    )
    if not r.ok:
        return []
    tag_list = {t.strip().lower() for t in tags.split(",") if t.strip()}
    similar = []
    for p in r.json().get("products", []):
        p_tags = {t.strip().lower() for t in p.get("tags", "").split(",") if t.strip()}
        if tag_list & p_tags:
            prices = [float(v["price"]) for v in p.get("variants", []) if v.get("price")]
            if prices:
                similar.append({
                    "title": p["title"],
                    "min_price": min(prices),
                    "max_price": max(prices),
                    "tags": p.get("tags", ""),
                })
    return similar[:5]


def fetch_price_history(api_base: str, headers: dict) -> dict:
    r = http_requests.get(
        f"{api_base}/products.json", headers=headers,
        params={"status": "active", "limit": 250, "fields": "variants"},
        timeout=10,
    )
    if not r.ok:
        return {}
    all_prices = [
        float(v["price"])
        for p in r.json().get("products", [])
        for v in p.get("variants", [])
        if v.get("price")
    ]
    if not all_prices:
        return {}
    return {
        "min": min(all_prices),
        "max": max(all_prices),
        "avg": round(sum(all_prices) / len(all_prices), 2),
        "count": len(all_prices),
    }


# ── Agent tool schema ─────────────────────────────────────────────────────────

AGENT_TOOLS = [
    {
        "name": "fetch_collections",
        "description": "Fetch all product collections/categories available in the store. Call this first to know what categories exist.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "fetch_similar_products",
        "description": "Find existing products with similar tags to understand pricing and positioning in the store.",
        "input_schema": {
            "type": "object",
            "properties": {
                "tags": {
                    "type": "string",
                    "description": "Comma-separated tags inferred from the product image, e.g. 'running,mesh,lightweight'",
                }
            },
            "required": ["tags"],
        },
    },
    {
        "name": "fetch_price_history",
        "description": "Get the historical price range (min, max, avg) from all existing active products in the store.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "submit_enrichment",
        "description": "Submit your final product enrichment after gathering all context. Call this last.",
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {
                    "type": "string",
                    "description": "Improved product title if the seller's title is vague or generic. Keep it concise (5-10 words), specific, and SEO-friendly. If the original title is already good, return it as-is.",
                },
                "description": {
                    "type": "string",
                    "description": "Compelling product description in 2-4 sentences. Highlight key features, material, and use-case.",
                },
                "category": {
                    "type": "string",
                    "description": "Best matching collection title from the store (must be one from fetch_collections results).",
                },
                "category_confidence": {
                    "type": "number",
                    "description": "Confidence score 0.0–1.0 for the category match. Be honest — lower score if ambiguous.",
                },
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Relevant searchable tags, e.g. ['running', 'lightweight', 'mesh', 'uk-sizing'].",
                },
                "suggested_price": {
                    "type": "number",
                    "description": "Suggested price in INR based on product quality and store price history.",
                },
                "price_confidence": {
                    "type": "number",
                    "description": "Confidence 0.0–1.0 that the suggested price fits the store's range.",
                },
                "image_quality": {
                    "type": "string",
                    "enum": ["acceptable", "poor"],
                    "description": "Assessment of image clarity and quality.",
                },
                "policy_check": {
                    "type": "string",
                    "enum": ["pass", "fail"],
                    "description": "Whether the product content passes basic content policy (no banned items, misleading claims, etc.).",
                },
                "review_reasons": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Specific reasons why a human should review this product. Empty list if confident in auto-publish.",
                },
                "seo_title": {
                    "type": "string",
                    "description": "SEO page title shown in Google results. Max 60 characters. Format: '<Product Name> | Mera Shelf'. Include the most important keyword first.",
                },
                "seo_description": {
                    "type": "string",
                    "description": "Meta description shown under the title in Google results. Max 155 characters. Compelling summary with key features and a soft call to action.",
                },
                "image_alt_text": {
                    "type": "string",
                    "description": "Descriptive alt text for the product image. Max 125 characters. Describe what is visually shown — material, colour, style, use-case. Do not start with 'image of'.",
                },
            },
            "required": [
                "title", "description", "category", "category_confidence", "tags",
                "suggested_price", "price_confidence", "image_quality",
                "policy_check", "review_reasons",
                "seo_title", "seo_description", "image_alt_text",
            ],
        },
    },
]


# ── Agentic loop ──────────────────────────────────────────────────────────────

def run_product_agent(product: dict, api_base: str, get_headers_fn) -> dict | None:
    """
    Run the AI agent to enrich a newly uploaded product.
    Returns the enrichment dict from submit_enrichment, or None on failure.
    """
    title = product.get("title", "Untitled")
    current_price = ""
    variants = product.get("variants", [])
    if variants:
        current_price = variants[0].get("price", "")

    # Build initial user message
    text_content = f"""You are a product enrichment agent for Mera Shelf, an Indian footwear/apparel store.

A seller has just uploaded a new product:
- Title: {title}
- Current description: {product.get("body_html") or "None"}
- Current price: {f'₹{current_price}' if current_price else 'Not set'}

Your workflow:
1. Call fetch_collections → learn available categories
2. Call fetch_similar_products with tags you infer from the image → understand pricing
3. Call fetch_price_history → learn the store's price range
4. Analyze the product image carefully
5. Call submit_enrichment with your final output

Rules:
- All prices in INR (₹)
- Only use category names that exist in fetch_collections results
- Be honest with confidence scores — lower score means seller reviews it
- If the image is unclear or you see multiple possible categories, reflect that in confidence
- seo_title: max 60 chars, format "<Product Name> | Mera Shelf", keyword-first
- seo_description: max 155 chars, natural language, include key material/use-case
- image_alt_text: max 125 chars, describe what is visually in the image (material, colour, style)"""

    content: list = [{"type": "text", "text": text_content}]

    # Attach product image if available
    images = product.get("images", [])
    if images:
        image_url = images[0].get("src", "")
        if image_url:
            try:
                resp = http_requests.get(image_url, timeout=15)
                resp.raise_for_status()
                media_type = resp.headers.get("content-type", "image/jpeg").split(";")[0]
                img_b64 = base64.standard_b64encode(resp.content).decode("utf-8")
                content.append({
                    "type": "image",
                    "source": {"type": "base64", "media_type": media_type, "data": img_b64},
                })
                log.info("agent.image_loaded", extra={"url": image_url[:80]})
            except Exception as e:
                log.warning("agent.image_load_failed", extra={"url": image_url[:80], "error": str(e)})
                content.append({"type": "text", "text": "[Note: Product image could not be loaded. Assess based on title only.]"})
    else:
        content.append({"type": "text", "text": "[Note: No product image provided. Assess based on title only.]"})

    messages = [{"role": "user", "content": content}]
    enrichment = None
    total_input_tokens = 0
    total_output_tokens = 0
    tool_calls_made: list[str] = []
    claude_call_count = 0

    try:
        import sentry_sdk
        _sentry_available = True
    except ImportError:
        _sentry_available = False

    # Agentic loop — Claude decides what tools to call
    for iteration in range(10):  # max 10 iterations
        claude_call_count += 1
        with (sentry_sdk.start_span(op="claude.api_call", name=f"Claude call #{iteration + 1}") if _sentry_available else _nullspan()) as span:
            response = claude.messages.create(
                model="claude-opus-4-6",
                max_tokens=2048,
                tools=AGENT_TOOLS,
                messages=messages,
            )
            total_input_tokens  += response.usage.input_tokens
            total_output_tokens += response.usage.output_tokens
            if _sentry_available and span:
                span.set_data("input_tokens", response.usage.input_tokens)
                span.set_data("output_tokens", response.usage.output_tokens)

        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason == "end_turn":
            break

        if response.stop_reason != "tool_use":
            break

        # Execute tool calls
        tool_results = []
        for block in response.content:
            if block.type != "tool_use":
                continue

            headers = get_headers_fn()
            name = block.name
            inp = block.input
            tool_calls_made.append(name)
            log.info("agent.tool_call", extra={"tool": name, "input": inp})

            with (sentry_sdk.start_span(op="agent.tool", name=name) if _sentry_available else _nullspan()):
                if name == "fetch_collections":
                    result = fetch_collections(api_base, headers)
                elif name == "fetch_similar_products":
                    result = fetch_similar_products(api_base, headers, inp.get("tags", ""))
                elif name == "fetch_price_history":
                    result = fetch_price_history(api_base, headers)
                elif name == "submit_enrichment":
                    enrichment = inp
                    result = {"status": "enrichment recorded"}
                else:
                    result = {"error": f"Unknown tool: {name}"}

            tool_results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": json.dumps(result),
            })

        messages.append({"role": "user", "content": tool_results})

        if enrichment:
            break

    # Claude Opus 4.6 pricing: $15/1M input, $75/1M output — converted to INR at ₹84
    cost_usd = (total_input_tokens / 1_000_000 * 15) + (total_output_tokens / 1_000_000 * 75)
    cost_inr = round(cost_usd * 84, 2)

    usage = {
        "input_tokens":   total_input_tokens,
        "output_tokens":  total_output_tokens,
        "total_tokens":   total_input_tokens + total_output_tokens,
        "claude_calls":   claude_call_count,
        "tool_calls":     tool_calls_made,
        "cost_usd":       round(cost_usd, 4),
        "cost_inr":       cost_inr,
    }
    log.info("agent.usage", extra=usage)
    return enrichment, usage


# ── Confidence gates ──────────────────────────────────────────────────────────

def evaluate_confidence(enrichment: dict, price_history: dict) -> tuple[bool, list[str]]:
    """
    Apply confidence gates. Returns (should_auto_publish, final_review_reasons).
    """
    reasons = list(enrichment.get("review_reasons", []))

    # Gate 1: Category confidence
    cat_conf = enrichment.get("category_confidence", 0)
    if cat_conf < CATEGORY_CONFIDENCE_THRESHOLD:
        reasons.append(
            f"Category confidence is {cat_conf:.0%} (below {CATEGORY_CONFIDENCE_THRESHOLD:.0%} threshold). "
            f"Agent suggested '{enrichment.get('category')}' but isn't certain."
        )

    # Gate 2: Image quality
    if enrichment.get("image_quality") == "poor":
        reasons.append("Product image is poor quality or unclear — please upload a clearer photo.")

    # Gate 3: Policy check
    if enrichment.get("policy_check") == "fail":
        reasons.append("Product content failed policy check — please review the title and description.")

    # Gate 4: Price within historical range
    if price_history and enrichment.get("suggested_price"):
        price = enrichment["suggested_price"]
        lo = price_history["min"] * (1 - PRICE_TOLERANCE)
        hi = price_history["max"] * (1 + PRICE_TOLERANCE)
        if not (lo <= price <= hi):
            reasons.append(
                f"Suggested price ₹{price:,.0f} is outside the store's typical range "
                f"(₹{price_history['min']:,.0f}–₹{price_history['max']:,.0f}). Please confirm."
            )

    auto_publish = len(reasons) == 0
    return auto_publish, reasons
