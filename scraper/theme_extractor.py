"""
Theme Extractor — converts a SiteAnalysis into Shopify-ready design tokens.

Takes the raw analysis JSON and produces a structured theme config that
maps directly to Shopify theme settings (settings_data.json format).
"""

import json
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

log = logging.getLogger(__name__)


# ── Simple heuristics to classify colors ──────────────────────────────────────

def _is_light(hex_color: str) -> bool:
    """Rough luminance check for a hex color."""
    hex_color = hex_color.lstrip("#")
    if len(hex_color) == 3:
        hex_color = "".join(c * 2 for c in hex_color)
    try:
        r, g, b = int(hex_color[0:2], 16), int(hex_color[2:4], 16), int(hex_color[4:6], 16)
        luminance = 0.299 * r + 0.587 * g + 0.114 * b
        return luminance > 186
    except ValueError:
        return False


def classify_colors(colors: list[str]) -> dict[str, str]:
    """
    Assign semantic roles to the top colors found on the site.
    Returns a dict: { "background": "#...", "text": "#...", "accent": "#...", ... }
    """
    hex_colors = [c for c in colors if c.startswith("#")]
    light = [c for c in hex_colors if _is_light(c)]
    dark = [c for c in hex_colors if not _is_light(c)]

    result = {
        "background": light[0] if light else "#FFFFFF",
        "text": dark[0] if dark else "#1A1A1A",
        "accent": dark[1] if len(dark) > 1 else "#000000",
        "secondary_background": light[1] if len(light) > 1 else "#F5F5F5",
        "border": light[2] if len(light) > 2 else "#E0E0E0",
    }
    return result


# ── Font mapping ──────────────────────────────────────────────────────────────

# Map scraped font names → safe Google Fonts / system font equivalents
_FONT_MAP = {
    "helvetica": "Helvetica Neue, Helvetica, Arial, sans-serif",
    "arial": "Arial, Helvetica, sans-serif",
    "georgia": "Georgia, serif",
    "times": "Times New Roman, Times, serif",
    "verdana": "Verdana, Geneva, sans-serif",
}

def map_fonts(fonts: list[str]) -> dict[str, str]:
    """
    Return heading_font and body_font for the theme.
    Falls back to safe system fonts if nothing useful is found.
    """
    clean = [f.strip("'\"").lower() for f in fonts if f]

    heading = next(
        (_FONT_MAP[k] for f in clean for k in _FONT_MAP if k in f),
        "Helvetica Neue, Helvetica, Arial, sans-serif"
    )
    body = clean[1] if len(clean) > 1 else "Arial, Helvetica, sans-serif"

    return {"heading_font": heading, "body_font": body}


# ── Main extractor ────────────────────────────────────────────────────────────

def extract_theme_tokens(analysis: dict) -> dict:
    """
    Given a SiteAnalysis dict, return a Shopify-compatible theme settings dict.
    """
    tokens = analysis.get("design_tokens", {})
    colors = classify_colors(tokens.get("colors", []))
    fonts = map_fonts(tokens.get("fonts", []))

    nav_labels = [item["label"] for item in analysis.get("navigation", [])]

    section_types = list({s["tag"] for s in analysis.get("sections", [])})

    return {
        "colors": colors,
        "typography": fonts,
        "navigation": {
            "items": nav_labels[:8],           # top 8 nav items
        },
        "layout": {
            "detected_sections": section_types,
            "has_hero": any(
                "hero" in " ".join(s.get("classes", [])).lower()
                or "banner" in " ".join(s.get("classes", [])).lower()
                for s in analysis.get("sections", [])
            ),
            "has_featured_collection": any(
                "collection" in s.get("text_preview", "").lower()
                for s in analysis.get("sections", [])
            ),
        },
        "meta": {
            "original_title": analysis.get("title", ""),
            "original_description": analysis.get("meta_description", ""),
        },
    }


def generate_shopify_settings(theme_tokens: dict) -> dict:
    """
    Produce a settings_data.json-compatible structure for a Shopify theme.
    """
    c = theme_tokens["colors"]
    t = theme_tokens["typography"]

    return {
        "current": {
            "colors_solid_button_labels": c["background"],
            "colors_accent_1": c["accent"],
            "colors_accent_2": c["text"],
            "colors_text": c["text"],
            "colors_outline_button_labels": c["text"],
            "colors_background_1": c["background"],
            "colors_background_2": c["secondary_background"],
            "colors_shadow": c["border"],
            "type_header_font": {"family": t["heading_font"], "style": "normal", "weight": 700},
            "type_body_font": {"family": t["body_font"], "style": "normal", "weight": 400},
            "type_body_font_size": 16,
            "type_header_font_size": 14,
            "page_width": 1200,
            "spacing_sections_desktop": 0,
            "spacing_sections_mobile": 0,
        }
    }


if __name__ == "__main__":
    from config import ANALYSIS_FILE, THEME_FILE

    with open(ANALYSIS_FILE, encoding="utf-8") as f:
        analysis = json.load(f)

    tokens = extract_theme_tokens(analysis)
    shopify_settings = generate_shopify_settings(tokens)

    os.makedirs(os.path.dirname(THEME_FILE), exist_ok=True)
    with open(THEME_FILE, "w", encoding="utf-8") as f:
        json.dump({"tokens": tokens, "shopify_settings": shopify_settings}, f, indent=2)

    print(json.dumps({"tokens": tokens, "shopify_settings": shopify_settings}, indent=2))
    log.info("Theme tokens saved to %s", THEME_FILE)
