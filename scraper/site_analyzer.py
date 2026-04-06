"""
Site Analyzer — scrapes publicly visible structure and design tokens
from a reference site to use as inspiration for a new Shopify store.

What it collects (all public HTML/CSS — no proprietary assets):
  - Color palette (hex values from CSS)
  - Typography (font families, sizes, weights)
  - Navigation structure (menu labels and hierarchy)
  - Page sections / layout patterns
  - Meta information (title, description format)
"""

import re
import json
import time
import logging
from dataclasses import dataclass, field, asdict
from typing import Optional
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from config import TARGET_SITE_URL, SCRAPER_DELAY_SECONDS, SCRAPER_USER_AGENT, SCRAPER_TIMEOUT

logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")
log = logging.getLogger(__name__)


@dataclass
class DesignTokens:
    colors: list[str] = field(default_factory=list)
    fonts: list[str] = field(default_factory=list)
    font_sizes: list[str] = field(default_factory=list)


@dataclass
class NavigationItem:
    label: str
    href: str
    children: list["NavigationItem"] = field(default_factory=list)


@dataclass
class PageSection:
    tag: str
    classes: list[str]
    role: Optional[str]
    text_preview: str


@dataclass
class SiteAnalysis:
    url: str
    title: str
    meta_description: str
    design_tokens: DesignTokens = field(default_factory=DesignTokens)
    navigation: list[NavigationItem] = field(default_factory=list)
    sections: list[PageSection] = field(default_factory=list)
    pages_analyzed: list[str] = field(default_factory=list)


class SiteAnalyzer:
    # Regex patterns for extracting CSS values
    _HEX_COLOR = re.compile(r"#(?:[0-9a-fA-F]{3}){1,2}\b")
    _RGB_COLOR = re.compile(r"rgba?\(\s*\d+\s*,\s*\d+\s*,\s*\d+(?:\s*,\s*[\d.]+)?\s*\)")
    _FONT_FAMILY = re.compile(r"font-family\s*:\s*([^;}{]+)")
    _FONT_SIZE = re.compile(r"font-size\s*:\s*([^;}{]+)")

    # Key pages to analyze beyond the homepage
    _EXTRA_PATHS = ["/collections", "/collections/all", "/pages/about"]

    def __init__(self, base_url: str = TARGET_SITE_URL):
        self.base_url = base_url.rstrip("/")
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": SCRAPER_USER_AGENT})

    # ── Public entry point ────────────────────────────────────────────────────

    def analyze(self) -> SiteAnalysis:
        log.info("Starting analysis of %s", self.base_url)
        soup, analysis = self._fetch_and_init(self.base_url)
        if soup is None:
            return analysis

        self._extract_design_tokens(soup, analysis)
        self._extract_navigation(soup, analysis)
        self._extract_sections(soup, analysis)

        # Analyze a few extra pages for richer data
        for path in self._EXTRA_PATHS:
            url = self.base_url + path
            extra_soup = self._fetch(url)
            if extra_soup:
                self._extract_sections(extra_soup, analysis)
                analysis.pages_analyzed.append(url)
                time.sleep(SCRAPER_DELAY_SECONDS)

        log.info("Analysis complete. Colors: %d  Fonts: %d  Sections: %d",
                 len(analysis.design_tokens.colors),
                 len(analysis.design_tokens.fonts),
                 len(analysis.sections))
        return analysis

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _fetch(self, url: str) -> Optional[BeautifulSoup]:
        try:
            resp = self.session.get(url, timeout=SCRAPER_TIMEOUT)
            resp.raise_for_status()
            return BeautifulSoup(resp.text, "html.parser")
        except requests.RequestException as e:
            log.warning("Could not fetch %s: %s", url, e)
            return None

    def _fetch_and_init(self, url: str) -> tuple[Optional[BeautifulSoup], SiteAnalysis]:
        soup = self._fetch(url)
        title = ""
        description = ""
        if soup:
            title = soup.title.string.strip() if soup.title else ""
            meta = soup.find("meta", attrs={"name": "description"})
            description = meta.get("content", "").strip() if meta else ""

        analysis = SiteAnalysis(
            url=url,
            title=title,
            meta_description=description,
            pages_analyzed=[url],
        )
        return soup, analysis

    def _extract_design_tokens(self, soup: BeautifulSoup, analysis: SiteAnalysis) -> None:
        """Extract colors and fonts from inline <style> tags and linked CSS."""
        css_text = "\n".join(tag.string for tag in soup.find_all("style") if tag.string)

        # Inline style attributes
        for tag in soup.find_all(style=True):
            css_text += "\n" + tag["style"]

        # Parse colors
        colors = set(self._HEX_COLOR.findall(css_text))
        colors |= set(self._RGB_COLOR.findall(css_text))
        analysis.design_tokens.colors = sorted(colors)[:30]  # cap at 30

        # Parse fonts
        fonts = []
        for match in self._FONT_FAMILY.findall(css_text):
            for font in match.split(","):
                font = font.strip().strip("'\"")
                if font and font.lower() not in ("inherit", "initial", "unset"):
                    fonts.append(font)
        analysis.design_tokens.fonts = list(dict.fromkeys(fonts))[:10]

        # Font sizes
        sizes = list(dict.fromkeys(
            s.strip() for s in self._FONT_SIZE.findall(css_text)
        ))
        analysis.design_tokens.font_sizes = sizes[:15]

    def _extract_navigation(self, soup: BeautifulSoup, analysis: SiteAnalysis) -> None:
        """Extract top-level navigation items."""
        nav = soup.find("nav") or soup.find(attrs={"role": "navigation"})
        if not nav:
            return

        for link in nav.find_all("a", href=True):
            label = link.get_text(strip=True)
            href = link["href"]
            if not href.startswith("http"):
                href = urljoin(self.base_url, href)
            if label:
                analysis.navigation.append(NavigationItem(label=label, href=href))

    def _extract_sections(self, soup: BeautifulSoup, analysis: SiteAnalysis) -> None:
        """Identify major page sections (header, main, footer, section tags)."""
        for tag in soup.find_all(["header", "main", "footer", "section", "article"]):
            classes = tag.get("class", [])
            role = tag.get("role")
            text = tag.get_text(separator=" ", strip=True)[:120]
            analysis.sections.append(PageSection(
                tag=tag.name,
                classes=classes,
                role=role,
                text_preview=text,
            ))


# ── Serialization helper ──────────────────────────────────────────────────────

def save_analysis(analysis: SiteAnalysis, path: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)

    def _serial(obj):
        if hasattr(obj, "__dataclass_fields__"):
            return asdict(obj)
        raise TypeError(f"Not serializable: {type(obj)}")

    with open(path, "w", encoding="utf-8") as f:
        json.dump(asdict(analysis), f, indent=2)
    log.info("Analysis saved to %s", path)


if __name__ == "__main__":
    from config import ANALYSIS_FILE
    analyzer = SiteAnalyzer()
    result = analyzer.analyze()
    save_analysis(result, ANALYSIS_FILE)
    print(json.dumps(asdict(result), indent=2))
