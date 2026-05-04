"""
Supabase persistence layer for Mera Shelf.

Table: enrichment_costs
Run this SQL once in your Supabase project (SQL Editor → New query):

    create table if not exists enrichment_costs (
        id            uuid primary key default gen_random_uuid(),
        ts            timestamptz not null,
        product_id    bigint,
        title         text,
        outcome       text,
        duration_s    numeric,
        input_tokens  integer,
        output_tokens integer,
        total_tokens  integer,
        claude_calls  integer,
        tool_calls    text[],
        cost_usd      numeric,
        cost_inr      numeric,
        created_at    timestamptz default now()
    );

    -- Optional: index for dashboard queries
    create index if not exists enrichment_costs_ts_idx on enrichment_costs (ts desc);

Usage:
    from db import insert_cost_record, load_cost_records
"""

import os
import json
from observability import get_logger

log = get_logger("db")

_client = None
_init_attempted = False


def _get_client():
    """Lazy-init the Supabase client. Returns None if not configured."""
    global _client, _init_attempted
    if _init_attempted:
        return _client
    _init_attempted = True

    url = os.environ.get("SUPABASE_URL", "")
    key = os.environ.get("SUPABASE_KEY", "")
    if not url or not key:
        log.info("db.supabase_not_configured", extra={"detail": "Cost ledger will be in-memory only"})
        return None

    try:
        from supabase import create_client
        _client = create_client(url, key)
        log.info("db.supabase_connected", extra={"url": url[:40]})
    except Exception as e:
        log.error("db.supabase_init_failed", extra={"error": str(e)})
        _client = None

    return _client


def insert_cost_record(record: dict) -> bool:
    """
    Persist one enrichment cost record to Supabase.
    Returns True on success, False if Supabase is not configured or fails.
    """
    client = _get_client()
    if not client:
        return False

    try:
        # Pass tool_calls as a plain Python list — Supabase handles text[] natively
        row = dict(record)
        row["tool_calls"] = list(row.get("tool_calls", []))
        client.table("enrichment_costs").insert(row).execute()
        log.info("db.cost_record_saved", extra={"product_id": record.get("product_id")})
        return True
    except Exception as e:
        log.error("db.insert_failed", extra={"error": str(e)})
        return False


def load_cost_records() -> list[dict]:
    """
    Load all enrichment cost records from Supabase, oldest first.
    Returns empty list if Supabase is not configured or fails.
    """
    client = _get_client()
    if not client:
        return []

    try:
        resp = client.table("enrichment_costs").select("*").order("ts", desc=False).execute()
        records = resp.data or []
        # Normalise: drop auto-generated Supabase columns not used by the dashboard
        cleaned = []
        for r in records:
            cleaned.append({
                "ts":            r.get("ts", ""),
                "product_id":    r.get("product_id"),
                "title":         r.get("title", ""),
                "outcome":       r.get("outcome", ""),
                "duration_s":    r.get("duration_s", 0),
                "input_tokens":  r.get("input_tokens", 0),
                "output_tokens": r.get("output_tokens", 0),
                "total_tokens":  r.get("total_tokens", 0),
                "claude_calls":  r.get("claude_calls", 0),
                "tool_calls":    r.get("tool_calls") or [],
                "cost_usd":      float(r.get("cost_usd", 0)),
                "cost_inr":      float(r.get("cost_inr", 0)),
            })
        log.info("db.cost_records_loaded", extra={"count": len(cleaned)})
        return cleaned
    except Exception as e:
        log.error("db.load_failed", extra={"error": str(e)})
        return []
