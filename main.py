"""
Main orchestration script — runs the full pipeline:

  Step 1: Analyze reference site (design tokens, nav, layout)
  Step 2: Extract Shopify-compatible theme tokens
  Step 3: Build Shopify store structure (collections, pages, menu)
  Step 4: Build and upload customized Dawn theme
  Step 5: Import sample products (replace with your catalog)

Usage:
  python main.py                    # Full pipeline
  python main.py --step analyze     # Only step 1+2
  python main.py --step store       # Only step 3
  python main.py --step theme       # Only step 4
  python main.py --step products    # Only step 5
"""

import argparse
import json
import logging
import os
import sys
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

from config import ANALYSIS_FILE, THEME_FILE, OUTPUT_DIR
from scraper.site_analyzer import SiteAnalyzer, save_analysis
from scraper.theme_extractor import extract_theme_tokens, generate_shopify_settings
from shopify.store_builder import StoreBuilder
from shopify.theme_builder import ThemeBuilder
from shopify.product_importer import ProductImporter, generate_sample_products


def step_analyze() -> dict:
    """Step 1+2: Scrape reference site and extract design tokens."""
    log.info("━━━ Step 1: Analyzing reference site ━━━")
    analyzer = SiteAnalyzer()
    analysis = analyzer.analyze()
    save_analysis(analysis, ANALYSIS_FILE)

    log.info("━━━ Step 2: Extracting theme tokens ━━━")
    from dataclasses import asdict
    analysis_dict = asdict(analysis)
    tokens = extract_theme_tokens(analysis_dict)
    shopify_settings = generate_shopify_settings(tokens)

    combined = {"tokens": tokens, "shopify_settings": shopify_settings}
    os.makedirs(os.path.dirname(THEME_FILE), exist_ok=True)
    with open(THEME_FILE, "w", encoding="utf-8") as f:
        json.dump(combined, f, indent=2)
    log.info("Theme tokens saved to %s", THEME_FILE)
    return combined


def step_store(theme_tokens: dict) -> tuple[list, dict]:
    """Step 3: Create collections, pages, and navigation in Shopify."""
    log.info("━━━ Step 3: Building Shopify store structure ━━━")
    builder = StoreBuilder()
    builder.print_store_summary()

    nav_items = theme_tokens.get("tokens", {}).get("navigation", {}).get("items", [])
    collections = builder.create_collections_from_nav(nav_items)
    builder.create_standard_pages()
    menu = builder.build_main_menu(collections)

    log.info("Store structure complete. Collections: %d", len(collections))
    return collections, menu


def step_theme(theme_tokens: dict) -> None:
    """Step 4: Build and upload a customized Dawn theme."""
    log.info("━━━ Step 4: Building and uploading theme ━━━")
    builder = ThemeBuilder(theme_tokens)
    theme_dir = builder.build()
    log.info("Theme built at %s", theme_dir)

    upload = input("\nUpload theme to Shopify now? [y/N]: ").strip().lower()
    if upload == "y":
        result = builder.upload_theme("AI Generated Theme")
        if result:
            log.info("Theme uploaded! ID=%s — preview in your Shopify admin.", result["id"])
    else:
        log.info("Skipping upload. You can upload manually from %s", theme_dir)


def step_products() -> None:
    """Step 5: Import products (sample placeholders by default)."""
    log.info("━━━ Step 5: Importing products ━━━")

    custom_file = Path("products.json")
    importer = ProductImporter()

    if custom_file.exists():
        log.info("Found products.json — importing your products...")
        imported, failed = importer.import_from_file(str(custom_file))
    else:
        log.info("No products.json found — importing 3 sample products as placeholders.")
        log.info("  Create products.json with your catalog to import real products.")
        samples = generate_sample_products(3)
        imported, failed = importer.import_from_list(samples)

    log.info("Products imported: %d  Failed: %d", len(imported), len(failed))


def load_theme_tokens() -> dict:
    if not Path(THEME_FILE).exists():
        log.error("Theme file not found: %s — run --step analyze first", THEME_FILE)
        sys.exit(1)
    with open(THEME_FILE, encoding="utf-8") as f:
        return json.load(f)


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Shopify AI Storefront Builder")
    parser.add_argument(
        "--step",
        choices=["analyze", "store", "theme", "products", "all"],
        default="all",
        help="Which pipeline step to run (default: all)",
    )
    args = parser.parse_args()

    if args.step in ("analyze", "all"):
        theme_tokens = step_analyze()
    else:
        theme_tokens = load_theme_tokens()

    if args.step in ("store", "all"):
        step_store(theme_tokens)

    if args.step in ("theme", "all"):
        step_theme(theme_tokens)

    if args.step in ("products", "all"):
        step_products()

    log.info("Pipeline finished.")


if __name__ == "__main__":
    main()
