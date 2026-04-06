"""
Theme Builder — generates a Shopify Dawn-based theme with custom design tokens.

Strategy:
  1. Downloads the Dawn theme (Shopify's free base theme) as a zip
  2. Patches settings_data.json with the extracted design tokens
  3. Generates custom CSS overrides
  4. Uploads the modified theme via the Shopify Admin API

Dawn theme repo: https://github.com/Shopify/dawn
"""

import json
import logging
import os
import time
import zipfile
from io import BytesIO
from pathlib import Path
from typing import Optional

import requests

import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from config import SHOPIFY_STORE_URL, SHOPIFY_ACCESS_TOKEN, SHOPIFY_API_VERSION, OUTPUT_DIR

log = logging.getLogger(__name__)

API_BASE = f"https://{SHOPIFY_STORE_URL}/admin/api/{SHOPIFY_API_VERSION}"
HEADERS = {"X-Shopify-Access-Token": SHOPIFY_ACCESS_TOKEN}

DAWN_LATEST_ZIP = "https://github.com/Shopify/dawn/archive/refs/heads/main.zip"


class ThemeBuilder:
    def __init__(self, theme_tokens: dict):
        self.tokens = theme_tokens
        self.shopify_settings = theme_tokens.get("shopify_settings", {})
        self.theme_dir = Path(OUTPUT_DIR) / "theme"

    # ── Build flow ────────────────────────────────────────────────────────────

    def build(self) -> Path:
        """Full build: download base theme, patch settings, write custom CSS."""
        log.info("Downloading Dawn base theme...")
        self._download_dawn()
        log.info("Patching settings_data.json...")
        self._patch_settings()
        log.info("Writing custom CSS overrides...")
        self._write_custom_css()
        log.info("Theme built at %s", self.theme_dir)
        return self.theme_dir

    def _download_dawn(self) -> None:
        resp = requests.get(DAWN_LATEST_ZIP, timeout=60)
        resp.raise_for_status()
        with zipfile.ZipFile(BytesIO(resp.content)) as zf:
            zf.extractall(OUTPUT_DIR)

        # Dawn extracts as "dawn-main/" — rename to "theme/"
        extracted = Path(OUTPUT_DIR) / "dawn-main"
        if extracted.exists():
            extracted.rename(self.theme_dir)
        elif not self.theme_dir.exists():
            raise FileNotFoundError("Dawn theme directory not found after extraction.")

    def _patch_settings(self) -> None:
        settings_path = self.theme_dir / "config" / "settings_data.json"
        if not settings_path.exists():
            log.warning("settings_data.json not found — skipping patch")
            return

        with open(settings_path, encoding="utf-8") as f:
            settings = json.load(f)

        # Merge our tokens into the "current" section
        current = settings.setdefault("current", {})
        current.update(self.shopify_settings.get("current", {}))

        with open(settings_path, "w", encoding="utf-8") as f:
            json.dump(settings, f, indent=2)

    def _write_custom_css(self) -> None:
        """Write a custom CSS file that applies the extracted design tokens."""
        c = self.tokens.get("tokens", {}).get("colors", {})
        t = self.tokens.get("tokens", {}).get("typography", {})

        css = f"""
/* ── Custom brand overrides (auto-generated) ── */
:root {{
  --color-background:           {c.get('background', '#ffffff')};
  --color-foreground:           {c.get('text', '#1a1a1a')};
  --color-accent:               {c.get('accent', '#000000')};
  --color-secondary-background: {c.get('secondary_background', '#f5f5f5')};
  --color-border:               {c.get('border', '#e0e0e0')};
  --font-heading-family:        {t.get('heading_font', 'Helvetica Neue, sans-serif')};
  --font-body-family:           {t.get('body_font', 'Arial, sans-serif')};
}}

body {{
  background-color: var(--color-background);
  color: var(--color-foreground);
  font-family: var(--font-body-family);
}}

h1, h2, h3, h4, h5, h6 {{
  font-family: var(--font-heading-family);
}}

.button, .btn {{
  background-color: var(--color-accent);
  color: var(--color-background);
  border-radius: 0;
  letter-spacing: 0.05em;
  text-transform: uppercase;
}}

.button:hover, .btn:hover {{
  opacity: 0.85;
}}
""".strip()

        assets_dir = self.theme_dir / "assets"
        assets_dir.mkdir(exist_ok=True)
        css_path = assets_dir / "brand-overrides.css"
        with open(css_path, "w", encoding="utf-8") as f:
            f.write(css)
        log.info("Custom CSS written to %s", css_path)

        # Inject the CSS file reference into theme.liquid
        layout_file = self.theme_dir / "layout" / "theme.liquid"
        if layout_file.exists():
            content = layout_file.read_text(encoding="utf-8")
            inject = "{{ 'brand-overrides.css' | asset_url | stylesheet_tag }}"
            if inject not in content:
                content = content.replace("</head>", f"  {inject}\n</head>", 1)
                layout_file.write_text(content, encoding="utf-8")
                log.info("Injected brand-overrides.css into theme.liquid")

    # ── Upload to Shopify ─────────────────────────────────────────────────────

    def upload_theme(self, theme_name: str = "AI Generated Theme") -> Optional[dict]:
        """
        Upload the built theme to Shopify.
        Returns the created theme object or None on failure.
        """
        if not self.theme_dir.exists():
            log.error("Theme not built yet — call build() first")
            return None

        log.info("Uploading theme files to Shopify...")
        session = requests.Session()
        session.headers.update(HEADERS)

        # Create theme entry
        create_resp = session.post(
            f"{API_BASE}/themes.json",
            json={"theme": {"name": theme_name, "role": "unpublished"}},
        )
        create_resp.raise_for_status()
        theme = create_resp.json()["theme"]
        theme_id = theme["id"]
        log.info("Theme created with id=%s", theme_id)

        # Upload each asset
        for file_path in self.theme_dir.rglob("*"):
            if not file_path.is_file():
                continue
            relative = file_path.relative_to(self.theme_dir)
            key = str(relative).replace(os.sep, "/")

            # Skip files Shopify doesn't accept
            if any(key.startswith(p) for p in [".git", "node_modules", ".github"]):
                continue

            try:
                content = file_path.read_text(encoding="utf-8")
                asset_type = "value"
            except UnicodeDecodeError:
                import base64
                content = base64.b64encode(file_path.read_bytes()).decode()
                asset_type = "attachment"

            resp = session.put(
                f"{API_BASE}/themes/{theme_id}/assets.json",
                json={"asset": {"key": key, asset_type: content}},
            )
            if resp.status_code == 429:
                time.sleep(2)
                resp = session.put(
                    f"{API_BASE}/themes/{theme_id}/assets.json",
                    json={"asset": {"key": key, asset_type: content}},
                )
            if not resp.ok:
                log.warning("Failed to upload %s: %s", key, resp.text[:200])
            else:
                log.debug("Uploaded %s", key)

        log.info("Theme upload complete. Theme ID: %s", theme_id)
        return theme
