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
import json
from pathlib import Path

from groq import Groq
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, FileResponse
from pydantic import BaseModel

# ── Config ────────────────────────────────────────────────────────────────────
GROQ_API_KEY      = os.environ.get("GROQ_API_KEY", "")
GROQ_MODEL        = "llama-3.3-70b-versatile"   # free tier, fast, high quality
SHOPIFY_STORE_URL = "https://myaistore-5.myshopify.com"
WIDGET_JS_PATH    = Path(__file__).parent / "widget.js"

SYSTEM_PROMPT = """You are a friendly and helpful AI shopping assistant for myaistore —
an online footwear store based in India. You help customers with:

- Product recommendations based on their needs (running, casual, gym, etc.)
- Size guidance (we use UK sizing: UK 6–11)
- Pricing information (all prices are in Indian Rupees ₹)
- Product details and comparisons

Our current product catalogue:
1. Classic Comfort Sneaker — ₹3,999 | Sizes UK 6–10 | Everyday casual sneaker
2. Lightweight Running Shoe — ₹5,499 | Sizes UK 6–10 | Daily runs and gym sessions
3. Easy Slip-On Loafer — ₹2,999 | Sizes UK 6–9 | Effortless slip-on for indoor/outdoor
4. High Performance Trainer — ₹6,999 | Sizes UK 7–11 | Cross-training and sprints

Store URL: https://myaistore-5.myshopify.com

Keep responses concise, warm, and helpful. If asked something outside footwear or
shopping, politely redirect the conversation back to the store. Always respond in
the same language the customer uses (English or Hindi)."""

# ── App ───────────────────────────────────────────────────────────────────────
app = FastAPI(title="myaistore Chatbot")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],   # lock down to SHOPIFY_STORE_URL in production
    allow_methods=["POST", "GET", "OPTIONS"],
    allow_headers=["*"],
)

client = Groq(api_key=GROQ_API_KEY)


# ── Request / Response models ─────────────────────────────────────────────────
class Message(BaseModel):
    role: str    # "user" or "assistant"
    content: str

class ChatRequest(BaseModel):
    messages: list[Message]   # full conversation history


# ── Chat endpoint (streaming SSE) ─────────────────────────────────────────────
@app.post("/chat")
async def chat(request: ChatRequest):
    """Stream Groq's response as Server-Sent Events."""

    # Groq uses OpenAI-compatible format: system msg goes in messages list
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    messages += [{"role": m.role, "content": m.content} for m in request.messages]

    def generate():
        stream = client.chat.completions.create(
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

    return StreamingResponse(generate(), media_type="text/event-stream")


# ── Serve widget JS ───────────────────────────────────────────────────────────
@app.get("/widget.js")
async def serve_widget():
    return FileResponse(WIDGET_JS_PATH, media_type="application/javascript")


# ── Health check ──────────────────────────────────────────────────────────────
@app.get("/health")
async def health():
    return {"status": "ok", "store": SHOPIFY_STORE_URL}
