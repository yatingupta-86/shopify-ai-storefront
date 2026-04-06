# Shopify AI Storefront Builder

Analyzes a reference site's public design (colors, fonts, nav structure) and builds a new, original Shopify store inspired by it.

> **Important:** This tool only reads publicly visible HTML/CSS. It does **not** copy copyrighted images, product copy, or brand assets. You must supply your own products and content.

## Setup

```bash
pip install -r requirements.txt
```

Edit `config.py` and set:
- `SHOPIFY_STORE_URL` — your store's `.myshopify.com` domain
- `SHOPIFY_ACCESS_TOKEN` — from Shopify Admin → Apps → Develop apps
- `TARGET_SITE_URL` — the site to use as design inspiration

## Shopify API Scopes Required
Create a Custom App in your store and grant these scopes:
- `write_products`
- `write_content`
- `write_themes`

## Usage

```bash
# Full pipeline (analyze → build store → upload theme → import products)
python main.py

# Individual steps
python main.py --step analyze     # Scrape reference site, save tokens to output/
python main.py --step store       # Create collections, pages, menus in Shopify
python main.py --step theme       # Build + upload Dawn-based theme
python main.py --step products    # Import products (from products.json or samples)
```

## Pipeline

```
1. SiteAnalyzer     → scrapes HTML/CSS → output/site_analysis.json
2. ThemeExtractor   → maps to design tokens → output/theme_design_tokens.json
3. StoreBuilder     → creates Shopify collections, pages, menus via API
4. ThemeBuilder     → patches Dawn theme with your colors/fonts, uploads to Shopify
5. ProductImporter  → imports your products.json (or sample placeholders)
```

## Adding your products

Create `products.json` in the project root:

```json
[
  {
    "title": "My Product",
    "vendor": "Your Brand",
    "product_type": "Footwear",
    "body_html": "<p>Description here.</p>",
    "tags": "tag1, tag2",
    "variants": [
      {"option1": "Size 8", "price": "95.00", "sku": "SKU-001", "inventory_quantity": 20}
    ],
    "options": [{"name": "Size", "values": ["Size 8"]}],
    "images": [{"src": "https://your-cdn.com/image.jpg", "alt": "Product image"}]
  }
]
```

## Project Structure

```
shopify-ai-storefront/
├── config.py                    # Credentials and settings
├── main.py                      # Orchestration entry point
├── requirements.txt
├── scraper/
│   ├── site_analyzer.py         # Scrape reference site structure
│   └── theme_extractor.py       # Convert to Shopify design tokens
├── shopify/
│   ├── store_builder.py         # Create collections, pages, menus
│   ├── theme_builder.py         # Build and upload Dawn theme
│   └── product_importer.py      # Import product catalog
└── output/                      # Generated files (gitignored)
    ├── site_analysis.json
    ├── theme_design_tokens.json
    └── theme/                   # Patched Dawn theme ready to upload
```
