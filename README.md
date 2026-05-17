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

## Multi-Agent Pipeline

The enrichment system uses 5 specialist agents, each right-sized to its task:

| Agent | Model | Input | Output |
|-------|-------|-------|--------|
| **Vision Agent** | Claude Opus 4.6 | Product image + title | product_type, material, color, style, use_case, image_quality |
| **Copy Agent** | Claude Sonnet 4.6 | Vision attributes + collections | title, description, tags, category, category_confidence |
| **Pricing Agent** | Claude Haiku 4.5 | Vision attributes + price history | suggested_price, price_confidence |
| **SEO Agent** | Claude Haiku 4.5 | Title + description | seo_title, seo_description, image_alt_text |
| **Policy Agent** | Claude Haiku 4.5 | Title + description + tags | policy_check, review_reasons |

**Execution order — parallel where possible:**

```
Image loads
     │
     ├── Vision Agent (Opus)       ──┐
     ├── fetch_collections           ├── parallel
     └── fetch_price_history        ──┘
                │
                ├── Copy Agent (Sonnet)   ──┐
                └── Pricing Agent (Haiku) ──┘ parallel
                            │
                            ├── SEO Agent (Haiku)    ──┐
                            └── Policy Agent (Haiku) ──┘ parallel
                                        │
                               Orchestrator merges all outputs
                                        │
                              Confidence gates evaluate:
                              • category_confidence ≥ 85%
                              • price within historical range ±20%
                              • image_quality = acceptable
                              • policy_check = pass
                                        │
                          Auto-publish      OR      Review queue
```

**Cost saving vs single Opus agent:** Copy (~5x cheaper), Pricing/SEO/Policy (~20x cheaper each)

---

## Architecture

```
Shopify Store
    │
    ├── product.created webhook ──► /webhooks/product-created
    │                                       │
    │                               Background task
    │                                       │
    │                          Multi-agent pipeline (see above)
    │                                       │
    │                          Confidence gates (85% threshold)
    │                                  ┌────┴────┐
    │                           Auto-publish   Review queue
    │                                           │
    │                                    Seller reviews
    │                                    (golden dataset
    │                                     captured here)
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
| Enrichment AI | Claude Opus 4.6 (vision), Sonnet 4.6 (copy), Haiku 4.5 (pricing/SEO/policy) |
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
├── evals/
│   └── run_evals.py        # Offline eval script — field change analysis + LLM judge + replay
├── db.py                   # Supabase client — cost ledger + golden dataset
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

## Offline Evals

The eval system uses the golden dataset (passively collected from seller approvals/rejections) to measure agent quality without deploying to production.

### Golden Dataset

Captured automatically every time a seller approves or rejects a product in the review queue. Each record stores:
- Original seller inputs (title, description, price, image URL)
- Store context the agent saw (collections, price history, similar products)
- AI-generated output
- Seller-edited gold output and which fields were changed
- Outcome (`approved` / `rejected`)

Run this SQL once in Supabase to create the table:

```sql
create table if not exists golden_dataset (
    id                   integer generated always as identity primary key,
    product_id           bigint,
    image_url            text,
    original_title       text,
    original_description text,
    original_price       text,
    agent_context        jsonb,
    ai_output            jsonb,
    gold_output          jsonb,
    fields_changed       text[],
    outcome              text,
    created_at           timestamptz default now()
);
```

### Running Evals

```bash
# Full eval — field change analysis + LLM-as-a-judge (uses Claude API)
python3 evals/run_evals.py

# Field change analysis only — free, no API calls
python3 evals/run_evals.py --no-judge

# Evaluate only the last 20 examples
python3 evals/run_evals.py --limit 20

# Replay mode — re-run agent on original inputs, compare old vs new scores
python3 evals/run_evals.py --replay
```

### Replay Mode

Re-runs the live agent code on each golden example's original inputs and scores both the old output (stored in the golden dataset) and the new output (from the replay) using the LLM judge. Reports improved / regressed / same per dimension.

Use this before deploying any prompt changes:

```
  Dimension                  Old avg   New avg      Change  Verdict
  ─────────────────────────  ───────  ───────  ─────────  ──────────
  overall                        3.0       4.0       +1.00  ✅ IMPROVED
  accuracy                       5.0       5.0       +0.00  ➡️  SAME
  description_quality            4.0       5.0       +1.00  ✅ IMPROVED
  category_fit                   3.0       3.0       +0.00  ➡️  SAME
  price_reasonableness           3.0       3.0       +0.00  ➡️  SAME
  seo_correctness                4.0       4.0       +0.00  ➡️  SAME
```

**Safe** — replay runs the agent read-only. No products are published or modified in Shopify.

**Cost** — ~$0.02–0.05 per example (agent run + two judge calls per example).

**Reliability** — results are meaningful at 20+ examples. With fewer examples, score swings reflect LLM sampling variance more than real quality differences.

### Interpreting Results

| Signal | What it means |
|--------|--------------|
| High `field_change_rate` on a field | Sellers frequently edit that field — that agent's prompt needs work |
| Judge score < 3.0 on a dimension | That dimension needs prompt improvement |
| `--replay` shows IMPROVED | Prompt change is safe to deploy |
| `--replay` shows REGRESSED | Revert the prompt change before deploying |

---

## Deployment

Deployed on [Render](https://render.com). The `Procfile` starts the server:

```
web: uvicorn chatbot.server:app --host 0.0.0.0 --port $PORT
```

Set all environment variables in Render → your service → Environment.
