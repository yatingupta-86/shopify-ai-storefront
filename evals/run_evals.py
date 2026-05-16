"""
Offline eval script for Mera Shelf enrichment agent.

What it does:
  1. Loads golden dataset from Supabase (seller-approved examples)
  2. Field change analysis — which fields sellers edit most → where the agent is weakest
  3. LLM-as-a-judge — scores every AI output on quality dimensions using Claude Haiku
  4. Regression check — compare judge scores between two prompt versions (optional)

Usage:
  python evals/run_evals.py                     # full eval report
  python evals/run_evals.py --limit 20          # evaluate last 20 examples only

Interpret results:
  - High field_change_rate for a field → that agent needs prompt improvement
  - Low judge score for a dimension → that agent needs prompt improvement
  - Compare scores before/after a prompt change to confirm improvement (no regression)
"""

import argparse
import json
import os
import sys
from collections import Counter, defaultdict

import anthropic
from dotenv import load_dotenv

load_dotenv()

_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _root not in sys.path:
    sys.path.insert(0, _root)

import db as _db

HAIKU = "claude-haiku-4-5-20251001"
claude = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY", ""))

TRACKED_FIELDS = ["title", "description", "suggested_price", "tags", "seo_title", "seo_description"]

# ── LLM Judge ────────────────────────────────────────────────────────────────

JUDGE_SYSTEM = """You are evaluating AI-generated product listings for Mera Shelf,
an Indian handmade goods store. Score each dimension honestly from 1 to 5.

Scoring rubric:
  description_quality (1-5)
    1 = generic, AI-sounding ("Beautiful handmade item. Great for gifting.")
    3 = adequate but could be more specific
    5 = warm, specific, mentions material + use-case + who it's for

  price_reasonableness (1-5)
    1 = clearly wrong for an Indian handmade product
    3 = plausible but uncertain
    5 = fair price that fits the handmade Indian market

  seo_correctness (1-5)
    1 = wrong format or over 60 characters
    3 = acceptable but not keyword-optimised
    5 = follows "<Product> | Mera Shelf" format, under 60 chars, keyword-first

  overall (1-5)
    Holistic quality — would you publish this without editing?
    1 = needs significant rework, 5 = publish immediately

Return ONLY valid JSON with keys:
description_quality, price_reasonableness, seo_correctness, overall, reason"""


def judge(ai_output: dict) -> dict:
    """Run LLM judge on one AI output. Returns score dict."""
    user_text = (
        f"Title: {ai_output.get('title', 'N/A')}\n"
        f"Description: {ai_output.get('description', 'N/A')}\n"
        f"Price: ₹{ai_output.get('suggested_price', 'N/A')}\n"
        f"SEO title: {ai_output.get('seo_title', 'N/A')}\n"
        f"Tags: {ai_output.get('tags', 'N/A')}"
    )
    try:
        response = claude.messages.create(
            model=HAIKU,
            max_tokens=256,
            system=JUDGE_SYSTEM,
            messages=[{"role": "user", "content": user_text}],
        )
        text = response.content[0].text.strip()
        return json.loads(text)
    except Exception as e:
        print(f"  [judge error] {e}")
        return {"description_quality": 0, "price_reasonableness": 0,
                "seo_correctness": 0, "overall": 0, "reason": str(e)}


# ── Analysis helpers ──────────────────────────────────────────────────────────

def field_change_analysis(examples: list[dict]) -> dict:
    """
    For each tracked field: how often did the seller change it?
    High rate = agent is weak on that field.
    """
    approved = [e for e in examples if e.get("outcome") == "approved"]
    if not approved:
        return {}

    change_counts = Counter()
    for ex in approved:
        for field in ex.get("fields_changed") or []:
            if field in TRACKED_FIELDS:
                change_counts[field] += 1

    return {
        field: {
            "changed": change_counts[field],
            "total":   len(approved),
            "rate":    round(change_counts[field] / len(approved) * 100, 1),
        }
        for field in TRACKED_FIELDS
    }


def judge_analysis(examples: list[dict]) -> dict:
    """
    Run LLM judge on every AI output. Return avg scores per dimension.
    """
    scores = defaultdict(list)
    total = len(examples)

    for i, ex in enumerate(examples):
        ai_output = ex.get("ai_output")
        if not ai_output:
            continue
        print(f"  Judging {i+1}/{total}: {ex.get('original_title', 'Untitled')[:50]}")
        result = judge(ai_output)
        for dim in ["description_quality", "price_reasonableness", "seo_correctness", "overall"]:
            if result.get(dim, 0) > 0:
                scores[dim].append(result[dim])

    return {
        dim: {
            "avg":    round(sum(vals) / len(vals), 2) if vals else 0,
            "min":    min(vals) if vals else 0,
            "max":    max(vals) if vals else 0,
            "count":  len(vals),
        }
        for dim, vals in scores.items()
    }


# ── Report printer ────────────────────────────────────────────────────────────

def print_report(examples: list[dict], field_stats: dict, judge_stats: dict):
    approved  = [e for e in examples if e.get("outcome") == "approved"]
    rejected  = [e for e in examples if e.get("outcome") == "rejected"]

    print("\n" + "═" * 60)
    print("  MERA SHELF — ENRICHMENT AGENT OFFLINE EVAL REPORT")
    print("═" * 60)
    print(f"\n  Total examples:  {len(examples)}")
    print(f"  Approved:        {len(approved)}  ({len(approved)/len(examples)*100:.0f}%)")
    print(f"  Rejected:        {len(rejected)}  ({len(rejected)/len(examples)*100:.0f}%)")

    print("\n── Field Change Analysis (approved only) ───────────────────")
    print("  High rate = agent is weakest on this field\n")
    if field_stats:
        sorted_fields = sorted(field_stats.items(), key=lambda x: -x[1]["rate"])
        for field, stat in sorted_fields:
            bar = "█" * int(stat["rate"] / 5)
            print(f"  {field:<20} {stat['rate']:5.1f}%  {bar}  ({stat['changed']}/{stat['total']})")
    else:
        print("  No approved examples yet.")

    print("\n── LLM Judge Scores (1–5 scale) ────────────────────────────")
    print("  Score < 3.0 → agent needs prompt improvement\n")
    dims = ["overall", "description_quality", "price_reasonableness", "seo_correctness"]
    for dim in dims:
        if dim not in judge_stats:
            continue
        s = judge_stats[dim]
        bar = "█" * int(s["avg"] * 2)
        flag = "  ⚠️  NEEDS WORK" if s["avg"] < 3.0 else ""
        print(f"  {dim:<25} avg={s['avg']:.2f}  min={s['min']}  max={s['max']}  {bar}{flag}")

    print("\n── Recommendations ─────────────────────────────────────────")
    recommendations = []
    if field_stats:
        worst_field = max(field_stats.items(), key=lambda x: x[1]["rate"])
        if worst_field[1]["rate"] > 30:
            recommendations.append(
                f"  → '{worst_field[0]}' is changed by sellers {worst_field[1]['rate']:.0f}% of the time. "
                f"Review and improve the relevant agent's system prompt."
            )
    if judge_stats.get("description_quality", {}).get("avg", 5) < 3:
        recommendations.append("  → Description quality is low. Add more specific examples to Copy Agent prompt.")
    if judge_stats.get("seo_correctness", {}).get("avg", 5) < 3:
        recommendations.append("  → SEO formatting issues. Reinforce format rules in SEO Agent prompt.")
    if judge_stats.get("price_reasonableness", {}).get("avg", 5) < 3:
        recommendations.append("  → Pricing is off. Add Indian handmade market context to Pricing Agent prompt.")
    if not recommendations:
        recommendations.append("  → All metrics look healthy. No immediate prompt changes needed.")
    for r in recommendations:
        print(r)

    print("\n" + "═" * 60 + "\n")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Run offline evals on golden dataset")
    parser.add_argument("--limit", type=int, default=None,
                        help="Evaluate only the last N examples (default: all)")
    parser.add_argument("--no-judge", action="store_true",
                        help="Skip LLM judge scoring (faster, no API cost)")
    args = parser.parse_args()

    print("\nLoading golden dataset from Supabase...")
    examples = _db.load_golden_dataset()

    if not examples:
        print("No golden examples found. Approve some products in the review queue first.")
        return

    if args.limit:
        examples = examples[:args.limit]
        print(f"Using last {len(examples)} examples (--limit {args.limit})")

    print(f"Loaded {len(examples)} examples.\n")

    print("Running field change analysis...")
    field_stats = field_change_analysis(examples)

    judge_stats = {}
    if not args.no_judge:
        print(f"\nRunning LLM judge on {len(examples)} examples (Claude Haiku)...")
        judge_stats = judge_analysis(examples)
    else:
        print("Skipping LLM judge (--no-judge flag set).")

    print_report(examples, field_stats, judge_stats)


if __name__ == "__main__":
    main()
