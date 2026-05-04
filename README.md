# Mera Shelf — AI-Powered Shopify Storefront

An AI-native e-commerce platform for Indian handmade goods sellers. Two AI systems work together to eliminate the manual work of listing products and answering customer questions.

---

## What It Does

### 1. Product Enrichment Agent
When a seller uploads a product photo to Shopify, an AI agent takes over:

- Fetches live store context (collections, similar products, price history) via autonomous tool calls
- Analyzes the product image using Claude's vision API
- Generates a product description, title, tags, SEO metadata, and price suggestion
- Applies multi-gate confidence scoring to decide: **auto-publish** or **queue for human review**

Gates that trigger human review:
- Category confidence < 85%
- Suggested price outside store's historical range (±20%)
- Poor image quality
- Content policy failure

### 2. Streaming AI Chatbot
A floating chat widget on the storefront powered by LLaMA 3.3 70B (via Groq):

- Answers product questions using the live Shopify catalogue
- Looks up real-time order status by order number
- Adds items to the customer's cart directly from the chat
- Streams responses token-by-token via Server-Sent Events

---

## Architecture

```
Shopify Store
    │
    ├── product.created webhook ──► /webhooks/product-created
    │                                       │
    │                               Background task
    │                                       │
    │                          ┌─── Agent (Claude Opus 4.6) ───┐
    │                          │   fetch_collections           │
    │                          │   fetch_similar_products      │
    │                          │   fetch_price_history         │
    │                          │   submit_enrichment           │
    │                          └───────────────────────────────┘
    │                                       │
    │                          Confidence gates (85% threshold)
    │                                  ┌────┴────┐
    │                           Auto-publish   Review queue
    │
    └── Customer browser ──► Chat widget (widget.js)
                                    │
                            POST /chat (SSE stream)
                                    │
                         LLaMA 3.3 70B via Groq
                         + live product catalogue
                         + order lookup
                         + cart add token
```

---

## Tech Stack

| Layer | Technology |
|-------|-----------|
| Backend | Python, FastAPI |
| Enrichment AI | Claude Opus 4.6 (Anthropic) — vision + tool use |
| Chatbot AI | LLaMA 3.3 70B (via Groq) |
| Storefront | Shopify — Horizon theme, Admin API, Webhooks |
| Database | Supabase (Postgres) — agent cost ledger |
| LLM Observability | Langfuse — traces, generations, tool spans |
| Error Tracking | Sentry — errors, performance, structured logs |
| Deployment | Render |

---

## Project Structure

```
shopify-ai-storefront/
├── chatbot/
│   ├── server.py           # FastAPI server — chat, webhooks, review queue, cost dashboard
│   ├── widget.js           # Chat bubble UI (runs in customer's browser)
│   └── inject_widget.py    # One-time: installs widget on Shopify storefront
├── agent/
│   ├── agent.py            # Agentic enrichment loop (Claude + tool use + Langfuse)
│   └── register_webhook.py # One-time: registers product-created webhook with Shopify
├── db.py                   # Supabase client — cost ledger persistence
├── observability.py        # Structured JSON logging + Sentry init
├── requirements.txt
└── Procfile                # Render deployment
```

---

## API Endpoints

| Method | URL | Auth | Purpose |
|--------|-----|------|---------|
| `POST` | `/chat` | — | Streaming chat (SSE) |
| `GET` | `/widget.js` | — | Serves chat widget to Shopify |
| `POST` | `/webhooks/product-created` | HMAC | Triggers enrichment agent |
| `GET` | `/review-queue/ui?token=` | ADMIN_TOKEN | Human review dashboard |
| `POST` | `/review-queue/{id}/approve` | — | Approve + publish product |
| `POST` | `/review-queue/{id}/reject` | — | Reject product |
| `GET` | `/costs?token=` | ADMIN_TOKEN | Agent cost dashboard |
| `GET` | `/health` | — | Health check |

---

## Setup

### 1. Clone and install

```bash
git clone https://github.com/yatingupta-86/shopify-ai-storefront.git
cd shopify-ai-storefront
pip install -r requirements.txt
```

### 2. Configure environment

```bash
cp .env.example .env
# Fill in your values
```

### 3. Shopify app setup

Create a Custom App in Shopify Admin → Apps → Develop apps with these scopes:
- `read_products`, `write_products`
- `read_orders`
- `write_script_tags`

### 4. One-time setup

```bash
# Install chat widget on your Shopify storefront
python chatbot/inject_widget.py

# Register the product-created webhook
python agent/register_webhook.py
```

### 5. Run locally

```bash
uvicorn chatbot.server:app --reload --port 8000
```

---

## Environment Variables

| Variable | Required | Purpose |
|----------|----------|---------|
| `SHOPIFY_STORE_URL` | ✅ | Your `.myshopify.com` domain |
| `SHOPIFY_CLIENT_ID` | ✅ | Dev app client ID |
| `SHOPIFY_CLIENT_SECRET` | ✅ | Dev app secret (webhook HMAC verification) |
| `SHOPIFY_ACCESS_TOKEN` | ✅ | Admin API access token |
| `SHOPIFY_API_VERSION` | ✅ | e.g. `2026-04` |
| `GROQ_API_KEY` | ✅ | Groq API key (chatbot) |
| `ANTHROPIC_API_KEY` | ✅ | Anthropic API key (enrichment agent) |
| `ADMIN_TOKEN` | ✅ | Password for review queue and cost dashboard |
| `SUPABASE_URL` | ⚡ | Supabase project URL (cost ledger persistence) |
| `SUPABASE_KEY` | ⚡ | Supabase service role key |
| `LANGFUSE_PUBLIC_KEY` | ⚡ | Langfuse public key (LLM tracing) |
| `LANGFUSE_SECRET_KEY` | ⚡ | Langfuse secret key |
| `LANGFUSE_HOST` | ⚡ | e.g. `https://us.cloud.langfuse.com` |
| `SENTRY_DSN` | ⚡ | Sentry DSN (error tracking) |
| `ENVIRONMENT` | ⚡ | `production` or `development` |

✅ Required &nbsp; ⚡ Optional but recommended

---

## Supabase Table

Run this once in Supabase SQL Editor to create the cost ledger table:

```sql
create table if not exists enrichment_costs (
    id            integer generated always as identity primary key,
    ts            timestamptz not null,
    product_id    bigint,
    title         text,
    outcome       text,
    duration_s    numeric,
    input_tokens  integer,
    output_tokens integer,
    total_tokens  integer,
    claude_calls  integer,
    tool_calls    text,
    cost_usd      numeric,
    cost_inr      numeric,
    created_at    timestamptz default now()
);
```

---

## Deployment

Deployed on [Render](https://render.com). The `Procfile` starts the server:

```
web: uvicorn chatbot.server:app --host 0.0.0.0 --port $PORT
```

Set all environment variables in Render → your service → Environment.
