# Shopify AI Storefront

Two AI-powered features for a Shopify store:

1. **Customer Chat Widget** — a floating chat button on the storefront. Customers ask questions and get AI answers (product info, order status, add-to-cart).
2. **Product Enrichment Agent** — when a new product is created in Shopify Admin, an AI agent analyzes the image, writes a description, suggests a title/price/tags, and either auto-publishes it or queues it for human review.

## Setup

```bash
pip install -r requirements.txt
```

Copy `.env.example` to `.env` and fill in your values.

## Shopify App Requirements

Create a Custom App in Shopify Admin → Apps → Develop apps and grant these scopes:
- `read_products`, `write_products`
- `read_orders`
- `write_script_tags`

## Running locally

```bash
uvicorn chatbot.server:app --reload
```

## One-time setup scripts

```bash
# Install the chat widget on your Shopify storefront
python chatbot/inject_widget.py

# Register the product-created webhook with Shopify
python agent/register_webhook.py
```

## Project Structure

```
shopify-ai-storefront/
├── chatbot/
│   ├── server.py           # FastAPI server — handles chat + webhooks
│   ├── widget.js           # Chat bubble UI (runs in customer's browser)
│   └── inject_widget.py    # One-time script to install widget on Shopify
├── agent/
│   ├── agent.py            # AI product enrichment agent (Claude + tool use)
│   └── register_webhook.py # One-time script to register Shopify webhook
├── config.py               # Reads secrets from .env
├── requirements.txt
└── Procfile                # Render deployment config
```

## Environment Variables

| Variable | Purpose |
|---|---|
| `SHOPIFY_STORE_URL` | Your `.myshopify.com` domain |
| `SHOPIFY_CLIENT_ID` | Dev app client ID |
| `SHOPIFY_CLIENT_SECRET` | Dev app secret (also used for webhook HMAC verification) |
| `SHOPIFY_ACCESS_TOKEN` | Admin API access token |
| `SHOPIFY_API_VERSION` | Shopify API version (default: `2026-04`) |
| `GROQ_API_KEY` | Groq API key for chat responses |
| `ANTHROPIC_API_KEY` | Anthropic API key for product image analysis |

## API Endpoints

| Method | URL | Purpose |
|---|---|---|
| `POST` | `/chat` | Customer chat (streamed via SSE) |
| `GET` | `/widget.js` | Serves the chat widget JS to Shopify |
| `POST` | `/webhooks/product-created` | Triggers agent on new product |
| `GET` | `/review-queue/ui` | Human review dashboard |
| `POST` | `/review-queue/{id}/approve` | Approve + publish a product |
| `POST` | `/review-queue/{id}/reject` | Reject a product |
| `GET` | `/health` | Health check |

## Deployment

Deployed on [Render](https://render.com) via `Procfile`. Set all environment variables in Render's dashboard.
