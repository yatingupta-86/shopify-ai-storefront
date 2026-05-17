"""
Offline eval script for Mera Shelf enrichment agent.

What it does:
  1. Loads golden dataset from Supabase (seller-approved/rejected examples)
  2. Field change analysis  — which fields sellers edit most → weakest agent
  3. LLM-as-a-judge        — scores AI output using FULL context:
       - product image (vision)
       - original title + description + price
       - store context (collections, price history, similar products)
       - AI generated output
     Judge model: Claude Sonnet 4.6 (vision-capable, can detect hallucination)

  --replay mode:
     Re-runs the actual agent code on each golden example's original inputs,
     scores both old output (from golden dataset) and new output (from replay),
     and reports whether your prompt changes improved or regressed quality.
     NO Shopify writes — read-only. Agent runs but does not publish anything.

Usage:
  python3 evals/run_evals.py                  # full eval with LLM judge
  python3 evals/run_evals.py --no-judge       # field change analysis only (free)
  python3 evals/run_evals.py --limit 20       # evaluate last 20 examples only
  python3 evals/run_evals.py --replay         # re-run agent + compare old vs new scores

Interpret results:
  High field_change_rate  → that agent's prompt needs improvement
  Low judge score (<3.0)  → that dimension needs improvement
  --replay IMPROVED       → prompt change made things better, safe to deploy
  --replay REGRESSED      → prompt change made things worse, revert it
"""

import argparse
import base64
import json
import os
import sys
from collections import Counter, defaultdict

import anthropic
import requests as http_requests
from dotenv import load_dotenv

load_dotenv()

_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _root not in sys.path:
    sys.path.insert(0, _root)

import db as _db

SONNET = "claude-sonnet-4-6"   # vision-capable — needed to evaluate image accuracy
claude  = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY", ""))

TRACKED_FIELDS = ["title", "description", "suggested_price", "tags", "seo_title", "seo_description"]

# ── LLM Judge ────────────────────────────────────────────────────────────────

JUDGE_SYSTEM = """You are an expert evaluator for Mera Shelf, an Indian handmade goods e-commerce store.

You will receive:
- The product image (what the seller uploaded)
- The original seller title (often vague like "test" or "new item")
- The original description and price (usually empty for new products)
- Store context: available categories, price history, similar products
- The AI-generated enrichment output

Your job is to score the AI output on 6 dimensions from 1 to 5.

── Scoring rubric ──────────────────────────────────────────────────────────────

accuracy (1-5) — does the AI output accurately describe what's in the image?
  1 = wrong product type, material, or colour described (hallucination)
  3 = mostly accurate but misses some visible details
  5 = perfectly matches what is visible in the image

description_quality (1-5) — is the description warm, specific, and useful?
  1 = generic AI-sounding ("Beautiful handmade item. Great for gifting.")
  3 = adequate but missing specifics about material, use-case, or who it's for
  5 = warm and specific — mentions material, use-case, target buyer, feels handcrafted

category_fit (1-5) — is the chosen category the best available option?
  1 = wrong category, a clearly better option was available in the store
  3 = acceptable but another category may have been a better fit
  5 = best possible category given the available collections

price_reasonableness (1-5) — is the price fair given store context?
  1 = clearly wrong — far outside the store's historical range without justification
  3 = within range but could be more precisely calibrated to similar products
  5 = well-reasoned, fits store range, comparable to similar products

seo_correctness (1-5) — does the SEO title follow the correct format?
  1 = wrong format or over 60 characters
  3 = acceptable but not keyword-optimised
  5 = follows "<Product Name> | Mera Shelf", keyword-first, under 60 characters

overall (1-5) — would you publish this listing without any editing?
  1 = needs significant rework before publishing
  3 = publishable but would benefit from minor edits
  5 = publish immediately, no changes needed

Return ONLY valid JSON with these exact keys:
accuracy, description_quality, category_fit, price_reasonableness, seo_correctness, overall, reason

The "reason" field should be 1-2 sentences explaining the overall score and the biggest weakness."""


def _load_image_b64(image_url: str) -> tuple[str, str] | None:
    """Download image and return (base64_data, media_type). Returns None on failure."""
    if not image_url:
        return None
    try:
        resp = http_requests.get(image_url, timeout=15)
        resp.raise_for_status()
        media_type = resp.headers.get("content-type", "image/jpeg").split(";")[0]
        b64 = base64.standard_b64encode(resp.content).decode("utf-8")
        return b64, media_type
    except Exception as e:
        print(f"  [image load failed] {e}")
        return None


def judge(example: dict) -> dict:
    """
    Run full input-aware LLM judge on one golden example.
    Passes image + original context + store context + AI output to Sonnet.
    """
    ai_output      = example.get("ai_output") or {}
    agent_context  = example.get("agent_context") or {}
    original_title = example.get("original_title", "N/A")
    original_desc  = example.get("original_description", "") or "None"
    original_price = example.get("original_price", "") or "Not set"
    image_url      = example.get("image_url", "")

    # Build user message content
    content = []

    # 1. Product image (vision input)
    img = _load_image_b64(image_url)
    if img:
        b64, media_type = img
        content.append({
            "type": "image",
            "source": {"type": "base64", "media_type": media_type, "data": b64},
        })
    else:
        content.append({"type": "text", "text": "[Product image unavailable]"})

    # 2. Full context text
    context_text = f"""── SELLER INPUT ────────────────────────────────
Original title:       {original_title}
Original description: {original_desc}
Original price:       {original_price}

── STORE CONTEXT (what the agent knew) ─────────
Available categories: {agent_context.get('collections', 'N/A')}
Price history:        {json.dumps(agent_context.get('price_history', {}))}
Similar products:     {json.dumps(agent_context.get('similar_products', []))}

── AI GENERATED OUTPUT ─────────────────────────
Title:           {ai_output.get('title', 'N/A')}
Description:     {ai_output.get('description', 'N/A')}
Category:        {ai_output.get('category', 'N/A')}
Suggested price: ₹{ai_output.get('suggested_price', 'N/A')}
Tags:            {ai_output.get('tags', 'N/A')}
SEO title:       {ai_output.get('seo_title', 'N/A')}
SEO description: {ai_output.get('seo_description', 'N/A')}
Image alt text:  {ai_output.get('image_alt_text', 'N/A')}

Now score this AI output on all 6 dimensions."""

    content.append({"type": "text", "text": context_text})

    try:
        response = claude.messages.create(
            model=SONNET,
            max_tokens=512,
            system=JUDGE_SYSTEM,
            messages=[{"role": "user", "content": content}],
        )
        text = response.content[0].text.strip()
        # Strip markdown code fences if present
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
        return json.loads(text.strip())
    except Exception as e:
        print(f"  [judge error] {e}")
        return {k: 0 for k in ["accuracy", "description_quality", "category_fit",
                                "price_reasonableness", "seo_correctness", "overall"]}


# ── Analysis helpers ──────────────────────────────────────────────────────────

def field_change_analysis(examples: list[dict]) -> dict:
    """Which fields do sellers edit most? High rate = weakest agent."""
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
    """Run LLM judge on every example. Return avg scores per dimension."""
    dims = ["accuracy", "description_quality", "category_fit",
            "price_reasonableness", "seo_correctness", "overall"]
    scores = defaultdict(list)
    total  = len(examples)

    for i, ex in enumerate(examples):
        if not ex.get("ai_output"):
            continue
        print(f"  Judging {i+1}/{total}: {ex.get('original_title', 'Untitled')[:50]}")
        result = judge(ex)
        for dim in dims:
            if result.get(dim, 0) > 0:
                scores[dim].append(result[dim])
        if result.get("reason"):
            print(f"    → {result['reason']}")

    return {
        dim: {
            "avg":   round(sum(vals) / len(vals), 2) if vals else 0,
            "min":   min(vals) if vals else 0,
            "max":   max(vals) if vals else 0,
            "count": len(vals),
        }
        for dim, vals in scores.items()
    }


# ── Report printer ────────────────────────────────────────────────────────────

def print_report(examples: list[dict], field_stats: dict, judge_stats: dict):
    approved = [e for e in examples if e.get("outcome") == "approved"]
    rejected = [e for e in examples if e.get("outcome") == "rejected"]

    print("\n" + "═" * 62)
    print("  MERA SHELF — ENRICHMENT AGENT OFFLINE EVAL REPORT")
    print("═" * 62)
    print(f"\n  Total examples : {len(examples)}")
    print(f"  Approved       : {len(approved)}  ({len(approved)/len(examples)*100:.0f}%)")
    print(f"  Rejected       : {len(rejected)}  ({len(rejected)/len(examples)*100:.0f}%)")

    print("\n── Field Change Analysis (approved only) ──────────────────")
    print("  High rate = agent is weakest on this field\n")
    if field_stats:
        for field, stat in sorted(field_stats.items(), key=lambda x: -x[1]["rate"]):
            bar  = "█" * int(stat["rate"] / 5)
            print(f"  {field:<22} {stat['rate']:5.1f}%  {bar}  ({stat['changed']}/{stat['total']})")
    else:
        print("  No approved examples yet.")

    if judge_stats:
        print("\n── LLM Judge Scores (1–5, Sonnet + vision) ────────────────")
        print("  Score < 3.0 → agent needs prompt improvement\n")
        dims = ["overall", "accuracy", "description_quality",
                "category_fit", "price_reasonableness", "seo_correctness"]
        for dim in dims:
            if dim not in judge_stats:
                continue
            s    = judge_stats[dim]
            bar  = "█" * int(s["avg"] * 2)
            flag = "  ⚠️  NEEDS WORK" if s["avg"] < 3.0 else ""
            print(f"  {dim:<25} avg={s['avg']:.2f}  min={s['min']}  max={s['max']}  {bar}{flag}")

    print("\n── Recommendations ────────────────────────────────────────")
    recs = []
    if field_stats:
        worst = max(field_stats.items(), key=lambda x: x[1]["rate"])
        if worst[1]["rate"] > 30:
            recs.append(f"  → '{worst[0]}' edited by sellers {worst[1]['rate']:.0f}% of the time — improve that agent's prompt.")
    if judge_stats:
        if judge_stats.get("accuracy", {}).get("avg", 5) < 3:
            recs.append("  → Low accuracy — Vision Agent may be hallucinating. Check image loading and prompt.")
        if judge_stats.get("description_quality", {}).get("avg", 5) < 3:
            recs.append("  → Poor description quality — add specific examples to Copy Agent prompt.")
        if judge_stats.get("seo_correctness", {}).get("avg", 5) < 3:
            recs.append("  → SEO format issues — reinforce format rules in SEO Agent prompt.")
        if judge_stats.get("price_reasonableness", {}).get("avg", 5) < 3:
            recs.append("  → Pricing off — add more Indian handmade market context to Pricing Agent prompt.")
        if judge_stats.get("category_fit", {}).get("avg", 5) < 3:
            recs.append("  → Category mismatch — add more specific collections to your Shopify store.")
    if not recs:
        recs.append("  → All metrics look healthy. No immediate changes needed.")
    for r in recs:
        print(r)

    print("\n" + "═" * 62 + "\n")


# ── Replay eval ──────────────────────────────────────────────────────────────

def _shopify_headers() -> dict:
    """Build Shopify headers from env vars for replay eval."""
    token = os.environ.get("SHOPIFY_ACCESS_TOKEN", "")
    return {"X-Shopify-Access-Token": token}


def _build_product_dict(example: dict) -> dict:
    """
    Reconstruct a minimal Shopify product dict from a golden example.
    Only contains fields the agent actually uses — no Shopify writes possible.
    """
    return {
        "id":        example.get("product_id"),
        "title":     example.get("original_title", "Untitled"),
        "body_html": example.get("original_description", ""),
        "variants":  [{"id": 0, "price": example.get("original_price", "")}],
        "images":    [{"src": example["image_url"]}] if example.get("image_url") else [],
    }


def replay_eval(examples: list[dict]) -> None:
    """
    Re-run the agent on each golden example's original inputs.
    Scores old output vs new output with the judge.
    Reports improved / regressed / same per dimension.

    SAFE: agent returns a dict only — no Shopify API writes happen here.
    """
    from agent.agent import run_product_agent

    api_base = (
        f"https://{os.environ.get('SHOPIFY_STORE_URL', '')}"
        f"/admin/api/{os.environ.get('SHOPIFY_API_VERSION', '2026-04')}"
    )

    dims = ["overall", "accuracy", "description_quality",
            "category_fit", "price_reasonableness", "seo_correctness"]

    old_scores_all = defaultdict(list)
    new_scores_all = defaultdict(list)
    results        = []

    print(f"\n{'─'*62}")
    print("  REPLAY MODE — re-running agent on golden inputs")
    print(f"  ⚠️  This calls Claude API — costs ~$0.02–0.05 per example")
    print(f"  ✅  Read-only — no Shopify writes")
    print(f"{'─'*62}\n")

    for i, example in enumerate(examples):
        title = example.get("original_title", "Untitled")
        print(f"[{i+1}/{len(examples)}] Replaying: {title[:50]}")

        # ── Score old output (from golden dataset) ─────────────────────────
        print("  Scoring old output (from golden dataset)...")
        old_scores = judge(example)

        # ── Re-run agent on original inputs ────────────────────────────────
        print("  Re-running agent on original inputs...")
        product = _build_product_dict(example)
        try:
            new_enrichment, _, _ = run_product_agent(
                product, api_base, _shopify_headers
            )
        except Exception as e:
            print(f"  ❌ Agent failed: {e}")
            continue

        if not new_enrichment:
            print("  ❌ Agent returned no enrichment — skipping")
            continue

        # ── Score new output ───────────────────────────────────────────────
        print("  Scoring new output...")
        new_example = dict(example)
        new_example["ai_output"] = {
            "title":           new_enrichment.get("title", ""),
            "description":     new_enrichment.get("description", ""),
            "suggested_price": new_enrichment.get("suggested_price"),
            "tags":            ", ".join(new_enrichment.get("tags", [])),
            "category":        new_enrichment.get("category", ""),
            "seo_title":       new_enrichment.get("seo_title", ""),
            "seo_description": new_enrichment.get("seo_description", ""),
            "image_alt_text":  new_enrichment.get("image_alt_text", ""),
        }
        new_scores = judge(new_example)

        # ── Per-example verdict ────────────────────────────────────────────
        old_overall = old_scores.get("overall", 0)
        new_overall = new_scores.get("overall", 0)
        if new_overall > old_overall:
            verdict = "✅ IMPROVED"
        elif new_overall < old_overall:
            verdict = "❌ REGRESSED"
        else:
            verdict = "➡️  SAME"

        print(f"  Overall: {old_overall} → {new_overall}  {verdict}")
        if new_scores.get("reason"):
            print(f"  Reason: {new_scores['reason']}")

        for dim in dims:
            if old_scores.get(dim, 0) > 0:
                old_scores_all[dim].append(old_scores[dim])
            if new_scores.get(dim, 0) > 0:
                new_scores_all[dim].append(new_scores[dim])

        results.append({
            "title":      title,
            "old_scores": old_scores,
            "new_scores": new_scores,
            "verdict":    verdict,
        })
        print()

    # ── Aggregate report ───────────────────────────────────────────────────
    print("\n" + "═" * 62)
    print("  REPLAY EVAL — AGGREGATE RESULTS")
    print("═" * 62)
    print(f"\n  {'Dimension':<25} {'Old avg':>8}  {'New avg':>8}  {'Change':>10}  Verdict")
    print(f"  {'─'*25}  {'─'*7}  {'─'*7}  {'─'*9}  {'─'*10}")

    for dim in dims:
        old_avg = round(sum(old_scores_all[dim]) / len(old_scores_all[dim]), 2) if old_scores_all[dim] else 0
        new_avg = round(sum(new_scores_all[dim]) / len(new_scores_all[dim]), 2) if new_scores_all[dim] else 0
        delta   = round(new_avg - old_avg, 2)
        if delta > 0:
            verdict = "✅ IMPROVED"
        elif delta < 0:
            verdict = "❌ REGRESSED"
        else:
            verdict = "➡️  SAME"
        print(f"  {dim:<25} {old_avg:>8}  {new_avg:>8}  {delta:>+10.2f}  {verdict}")

    improved  = sum(1 for r in results if "IMPROVED"  in r["verdict"])
    regressed = sum(1 for r in results if "REGRESSED" in r["verdict"])
    same      = sum(1 for r in results if "SAME"      in r["verdict"])
    print(f"\n  Examples: {improved} improved · {regressed} regressed · {same} same")

    if regressed == 0 and improved > 0:
        print("\n  ✅ Prompt change looks SAFE TO DEPLOY — no regressions detected.")
    elif regressed > 0:
        print(f"\n  ⚠️  {regressed} regression(s) detected — review before deploying.")
    else:
        print("\n  ➡️  No change detected — prompt may need more significant adjustment.")

    print("\n" + "═" * 62 + "\n")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit",    type=int, default=None, help="Evaluate last N examples only")
    parser.add_argument("--no-judge", action="store_true",    help="Skip LLM judge (free, instant)")
    parser.add_argument("--replay",   action="store_true",
                        help="Re-run agent on golden inputs and compare old vs new scores")
    args = parser.parse_args()

    print("\nLoading golden dataset from Supabase...")
    examples = _db.load_golden_dataset()

    if not examples:
        print("No golden examples found. Approve some products in the review queue first.")
        return

    if args.limit:
        examples = examples[:args.limit]

    print(f"Loaded {len(examples)} examples.\n")

    if args.replay:
        replay_eval(examples)
        return

    print("Running field change analysis...")
    field_stats = field_change_analysis(examples)

    judge_stats = {}
    if not args.no_judge:
        print(f"\nRunning LLM judge on {len(examples)} examples (Claude Sonnet — vision-aware)...")
        judge_stats = judge_analysis(examples)
    else:
        print("Skipping LLM judge (--no-judge).")

    print_report(examples, field_stats, judge_stats)


if __name__ == "__main__":
    main()
