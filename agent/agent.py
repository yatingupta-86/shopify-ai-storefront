"""
Multi-agent product enrichment pipeline for Mera Shelf.

When a seller uploads a product to Shopify:
  1. Vision Agent    (Claude Opus 4.6)    — analyzes product image
  2. Context fetch   (Shopify API)        — collections, similar products, price history (parallel with vision)
  3. Copy Agent      (Claude Sonnet 4.6)  — title, description, tags, category
     Pricing Agent   (Claude Haiku 4.5)   — suggested price             ┐ all run
     SEO Agent       (Claude Haiku 4.5)   — seo_title, meta, alt text  ─┤ in
     Policy Agent    (Claude Haiku 4.5)   — content policy check        ┘ parallel
  4. Orchestrator merges results → confidence gates → auto-publish or review queue

Cost saving vs single Opus agent:
  Copy    → Sonnet (~5x cheaper than Opus)
  Pricing → Haiku  (~20x cheaper than Opus)
  SEO     → Haiku  (~20x cheaper than Opus)
  Policy  → Haiku  (~20x cheaper than Opus)
"""

import base64
import json
import os
import sys
import warnings
import concurrent.futures
warnings.filterwarnings("ignore", category=SyntaxWarning, module="langfuse")

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

# ── Model IDs ─────────────────────────────────────────────────────────────────
MODEL_OPUS   = "claude-opus-4-6"
MODEL_SONNET = "claude-sonnet-4-6"
MODEL_HAIKU  = "claude-haiku-4-5-20251001"

# ── Per-model pricing (USD per 1M tokens) ─────────────────────────────────────
MODEL_PRICING = {
    MODEL_OPUS:   {"input": 15.0,  "output": 75.0},
    MODEL_SONNET: {"input": 3.0,   "output": 15.0},
    MODEL_HAIKU:  {"input": 0.80,  "output": 4.0},
}

# ── Confidence thresholds ─────────────────────────────────────────────────────
CATEGORY_CONFIDENCE_THRESHOLD = 0.85
PRICE_TOLERANCE = 0.20   # allow 20% outside historical range


# ── Langfuse (lazy init — no-op if keys not set) ──────────────────────────────

_langfuse = None

def _get_langfuse():
    global _langfuse
    if _langfuse is not None:
        return _langfuse
    pk = os.environ.get("LANGFUSE_PUBLIC_KEY", "")
    sk = os.environ.get("LANGFUSE_SECRET_KEY", "")
    if not pk or not sk:
        return None
    try:
        from langfuse import Langfuse
        _langfuse = Langfuse(
            public_key=pk,
            secret_key=sk,
            host=os.environ.get("LANGFUSE_HOST", "https://us.cloud.langfuse.com"),
        )
        _langfuse.auth_check()
        log.info("langfuse.connected")
    except Exception as e:
        log.warning("langfuse.init_failed", extra={"error": str(e)})
        _langfuse = None
    return _langfuse


# ── Shopify context fetchers ──────────────────────────────────────────────────

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


# ── Token + cost accumulator ──────────────────────────────────────────────────

class _UsageTracker:
    def __init__(self):
        self.input_tokens  = 0
        self.output_tokens = 0
        self.cost_usd      = 0.0
        self.agents        = []

    def record(self, model: str, response, agent_name: str):
        inp  = response.usage.input_tokens
        out  = response.usage.output_tokens
        p    = MODEL_PRICING.get(model, {"input": 15.0, "output": 75.0})
        cost = (inp / 1_000_000 * p["input"]) + (out / 1_000_000 * p["output"])
        self.input_tokens  += inp
        self.output_tokens += out
        self.cost_usd      += cost
        self.agents.append(agent_name)
        log.info("agent.call", extra={
            "agent": agent_name, "model": model,
            "input_tokens": inp, "output_tokens": out,
            "cost_usd": round(cost, 6),
        })

    def summary(self) -> dict:
        cost_inr = round(self.cost_usd * 84, 2)
        return {
            "input_tokens":  self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens":  self.input_tokens + self.output_tokens,
            "claude_calls":  len(self.agents),
            "tool_calls":    self.agents,
            "cost_usd":      round(self.cost_usd, 4),
            "cost_inr":      cost_inr,
        }


# ── Helper: call Claude and extract JSON via tool use ─────────────────────────

def _call_structured(model: str, system: str, user_content: list, tool_name: str,
                     tool_schema: dict, tracker: _UsageTracker,
                     lf_trace=None) -> dict:
    """
    Call Claude with a single output tool. Returns the tool input dict.
    Uses tool_use to guarantee structured JSON output.
    """
    lf_gen = None
    try:
        if lf_trace:
            lf_gen = lf_trace.generation(
                name=tool_name, model=model,
                input={"system": system[:200]},
            )
    except Exception:
        pass

    response = claude.messages.create(
        model=model,
        max_tokens=1024,
        system=system,
        tools=[{"name": tool_name, "description": "Submit structured output.", "input_schema": tool_schema}],
        tool_choice={"type": "tool", "name": tool_name},
        messages=[{"role": "user", "content": user_content}],
    )
    tracker.record(model, response, tool_name)

    try:
        if lf_gen:
            lf_gen.end(
                output=tool_name,
                usage={"input": response.usage.input_tokens, "output": response.usage.output_tokens},
            )
    except Exception:
        pass

    for block in response.content:
        if block.type == "tool_use" and block.name == tool_name:
            return block.input
    return {}


# ── Specialist agents ─────────────────────────────────────────────────────────

def _vision_agent(title: str, image_content: list, tracker: _UsageTracker,
                  lf_trace=None) -> dict:
    """
    Claude Opus — analyze product image and return structured attributes.
    This is the only agent that needs Opus-level vision reasoning.
    """
    system = (
        "You are a product vision analyst for Mera Shelf, an Indian handmade goods store. "
        "Analyze the product image carefully and extract structured attributes."
    )
    user_content = [
        {"type": "text", "text": f"Product title: {title}\n\nAnalyze this product image and extract attributes."},
        *image_content,
    ]
    schema = {
        "type": "object",
        "properties": {
            "product_type": {"type": "string", "description": "e.g. crochet toy, wall hanging, tote bag"},
            "material":     {"type": "string", "description": "e.g. cotton yarn, jute, wool"},
            "color":        {"type": "string", "description": "Primary color(s) visible"},
            "style":        {"type": "string", "description": "e.g. bohemian, minimalist, traditional"},
            "use_case":     {"type": "string", "description": "e.g. gifting, home decor, kids toy"},
            "inferred_tags":{"type": "array", "items": {"type": "string"},
                             "description": "5-10 searchable tags inferred from the image"},
            "image_quality":{"type": "string", "enum": ["acceptable", "poor"]},
        },
        "required": ["product_type", "material", "color", "style", "use_case", "inferred_tags", "image_quality"],
    }
    return _call_structured(MODEL_OPUS, system, user_content, "vision_output", schema, tracker, lf_trace)


def _copy_agent(title: str, attributes: dict, collections: list[dict],
                tracker: _UsageTracker, lf_trace=None) -> dict:
    """
    Claude Sonnet — write title, description, tags, category.
    Creative writing task — Sonnet quality at ~5x lower cost than Opus.
    """
    system = (
        "You are a product copywriter for Mera Shelf, an Indian handmade goods store. "
        "Write compelling, honest product copy based on the provided attributes. "
        "All categories must be chosen from the available collections list."
    )
    collection_names = [c["title"] for c in collections]
    user_content = [{
        "type": "text",
        "text": (
            f"Original title: {title}\n"
            f"Product attributes: {json.dumps(attributes)}\n"
            f"Available categories: {collection_names}\n\n"
            "Write the product copy."
        ),
    }]
    schema = {
        "type": "object",
        "properties": {
            "title":               {"type": "string", "description": "Concise, SEO-friendly product title (5-10 words)"},
            "description":         {"type": "string", "description": "2-4 sentence product description highlighting features, material, use-case"},
            "tags":                {"type": "array", "items": {"type": "string"}, "description": "8-12 searchable tags"},
            "category":            {"type": "string", "description": "Best matching category from the available list"},
            "category_confidence": {"type": "number", "description": "Confidence 0.0-1.0 for the category match"},
        },
        "required": ["title", "description", "tags", "category", "category_confidence"],
    }
    return _call_structured(MODEL_SONNET, system, user_content, "copy_output", schema, tracker, lf_trace)


def _pricing_agent(title: str, attributes: dict, similar_products: list[dict],
                   price_history: dict, current_price: str,
                   tracker: _UsageTracker, lf_trace=None) -> dict:
    """
    Claude Haiku — suggest price based on store context.
    Simple numerical reasoning — Haiku is sufficient and ~20x cheaper than Opus.
    """
    system = (
        "You are a pricing analyst for Mera Shelf, an Indian handmade goods store. "
        "Suggest a fair price in INR based on product attributes and store pricing context."
    )
    user_content = [{
        "type": "text",
        "text": (
            f"Product: {title}\n"
            f"Attributes: {json.dumps(attributes)}\n"
            f"Current price: {f'₹{current_price}' if current_price else 'Not set'}\n"
            f"Similar products: {json.dumps(similar_products)}\n"
            f"Store price history: {json.dumps(price_history)}\n\n"
            "Suggest a price in INR."
        ),
    }]
    schema = {
        "type": "object",
        "properties": {
            "suggested_price":  {"type": "number", "description": "Suggested price in INR"},
            "price_confidence": {"type": "number", "description": "Confidence 0.0-1.0 that price fits the store range"},
        },
        "required": ["suggested_price", "price_confidence"],
    }
    return _call_structured(MODEL_HAIKU, system, user_content, "pricing_output", schema, tracker, lf_trace)


def _seo_agent(title: str, description: str, product_type: str,
               tracker: _UsageTracker, lf_trace=None) -> dict:
    """
    Claude Haiku — generate SEO fields.
    Templated / rule-based task — Haiku is sufficient and ~20x cheaper than Opus.
    """
    system = (
        "You are an SEO specialist for Mera Shelf, an Indian handmade goods store. "
        "Generate SEO metadata following these rules:\n"
        "- seo_title: max 60 chars, format '<Product Name> | Mera Shelf', keyword-first\n"
        "- seo_description: max 155 chars, natural language with key material/use-case, soft CTA\n"
        "- image_alt_text: max 125 chars, describe what is visually shown — material, colour, style. Do not start with 'image of'."
    )
    user_content = [{
        "type": "text",
        "text": f"Product title: {title}\nDescription: {description}\nProduct type: {product_type}",
    }]
    schema = {
        "type": "object",
        "properties": {
            "seo_title":       {"type": "string"},
            "seo_description": {"type": "string"},
            "image_alt_text":  {"type": "string"},
        },
        "required": ["seo_title", "seo_description", "image_alt_text"],
    }
    return _call_structured(MODEL_HAIKU, system, user_content, "seo_output", schema, tracker, lf_trace)


def _policy_agent(title: str, description: str, tags: list[str],
                  tracker: _UsageTracker, lf_trace=None) -> dict:
    """
    Claude Haiku — content policy check.
    Simple classification task — Haiku is sufficient and ~20x cheaper than Opus.
    """
    system = (
        "You are a content policy reviewer for an Indian e-commerce store. "
        "Check whether the product listing passes basic content policy. "
        "Fail if: banned/illegal items, misleading health claims, adult content, "
        "counterfeit brand names, or hate speech."
    )
    user_content = [{
        "type": "text",
        "text": f"Title: {title}\nDescription: {description}\nTags: {', '.join(tags)}",
    }]
    schema = {
        "type": "object",
        "properties": {
            "policy_check":   {"type": "string", "enum": ["pass", "fail"]},
            "review_reasons": {"type": "array", "items": {"type": "string"},
                               "description": "Reasons for human review. Empty if confident."},
        },
        "required": ["policy_check", "review_reasons"],
    }
    return _call_structured(MODEL_HAIKU, system, user_content, "policy_output", schema, tracker, lf_trace)


# ── Orchestrator ──────────────────────────────────────────────────────────────

def run_product_agent(product: dict, api_base: str, get_headers_fn) -> tuple[dict | None, dict]:
    """
    Orchestrate all specialist agents to enrich a newly uploaded product.
    Returns (enrichment_dict, usage_dict).
    """
    title = product.get("title", "Untitled")
    current_price = ""
    variants = product.get("variants", [])
    if variants:
        current_price = variants[0].get("price", "")

    tracker = _UsageTracker()

    try:
        import sentry_sdk
        _sentry_available = True
    except ImportError:
        _sentry_available = False

    # Start Langfuse trace
    lf = _get_langfuse()
    lf_trace = None
    try:
        if lf:
            lf_trace = lf.trace(
                name="product-enrichment",
                input={"title": title, "product_id": product.get("id")},
                metadata={"store": api_base},
            )
    except Exception as e:
        log.warning("langfuse.trace_failed", extra={"error": str(e)})

    # ── Step 1: Load product image ─────────────────────────────────────────────
    image_content = []
    images = product.get("images", [])
    if images:
        image_url = images[0].get("src", "")
        if image_url:
            try:
                resp = http_requests.get(image_url, timeout=15)
                resp.raise_for_status()
                media_type = resp.headers.get("content-type", "image/jpeg").split(";")[0]
                img_b64 = base64.standard_b64encode(resp.content).decode("utf-8")
                image_content = [{"type": "image", "source": {
                    "type": "base64", "media_type": media_type, "data": img_b64,
                }}]
                log.info("agent.image_loaded", extra={"url": image_url[:80]})
            except Exception as e:
                log.warning("agent.image_load_failed", extra={"url": image_url[:80], "error": str(e)})
                image_content = [{"type": "text", "text": "[Image unavailable — assess from title only]"}]
    else:
        image_content = [{"type": "text", "text": "[No image provided — assess from title only]"}]

    # ── Step 2: Vision agent + context fetch run in parallel ───────────────────
    headers = get_headers_fn()

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:
        f_vision      = ex.submit(_vision_agent, title, image_content, tracker, lf_trace)
        f_collections = ex.submit(fetch_collections, api_base, headers)
        f_price_hist  = ex.submit(fetch_price_history, api_base, headers)

        attributes    = f_vision.result()
        collections   = f_collections.result()
        price_history = f_price_hist.result()

    log.info("agent.vision_done", extra={"attributes": attributes})

    # Fetch similar products using tags from vision output
    inferred_tags = ", ".join(attributes.get("inferred_tags", []))
    similar_products = fetch_similar_products(api_base, get_headers_fn(), inferred_tags)

    # ── Step 3: Copy + Pricing + SEO + Policy run in parallel ─────────────────
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:
        f_copy   = ex.submit(_copy_agent, title, attributes, collections, tracker, lf_trace)
        f_price  = ex.submit(_pricing_agent, title, attributes, similar_products,
                             price_history, current_price, tracker, lf_trace)

        copy_result  = f_copy.result()
        price_result = f_price.result()

    # SEO and Policy need copy output — run after copy
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
        f_seo    = ex.submit(_seo_agent, copy_result.get("title", title),
                             copy_result.get("description", ""), attributes.get("product_type", ""),
                             tracker, lf_trace)
        f_policy = ex.submit(_policy_agent, copy_result.get("title", title),
                             copy_result.get("description", ""), copy_result.get("tags", []),
                             tracker, lf_trace)

        seo_result    = f_seo.result()
        policy_result = f_policy.result()

    # ── Step 4: Merge all agent outputs ───────────────────────────────────────
    enrichment = {
        # Copy agent
        "title":               copy_result.get("title", title),
        "description":         copy_result.get("description", ""),
        "tags":                copy_result.get("tags", []),
        "category":            copy_result.get("category", ""),
        "category_confidence": copy_result.get("category_confidence", 0.0),
        # Pricing agent
        "suggested_price":     price_result.get("suggested_price", 0),
        "price_confidence":    price_result.get("price_confidence", 0.0),
        # Vision agent
        "image_quality":       attributes.get("image_quality", "acceptable"),
        # SEO agent
        "seo_title":           seo_result.get("seo_title", ""),
        "seo_description":     seo_result.get("seo_description", ""),
        "image_alt_text":      seo_result.get("image_alt_text", ""),
        # Policy agent
        "policy_check":        policy_result.get("policy_check", "pass"),
        "review_reasons":      policy_result.get("review_reasons", []),
    }

    usage = tracker.summary()
    log.info("agent.usage", extra=usage)

    # Close Langfuse trace
    try:
        if lf_trace:
            lf_trace.update(output=enrichment, metadata=usage)
            lf.flush()
    except Exception:
        pass

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
