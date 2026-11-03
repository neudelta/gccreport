"""ET GCC (gcc.economictimes.indiatimes.com) - listing scrape + article JSON-LD."""
import json
import re
from datetime import date
from urllib.parse import urlsplit, urlunsplit

from .base import EVENT_TITLE, Doc, in_window, parse_any_date

BASE = "https://gcc.economictimes.indiatimes.com"
LISTING = f"{BASE}/news"
# /news only shows a handful of recent features. Openings and expansions are filed under workplace-transformation,
# so read that section's first two pages as well.
LISTINGS = (
    f"{BASE}/news/workplace-transformation",
    f"{BASE}/news/workplace-transformation/2",
    LISTING,
)
ARTICLE_RE = re.compile(r"^/news/[^/]+/[^/]+/\d+$")
MAX_ARTICLES = 40


def parse_listing(anchors: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """anchors: (text, href). Returns unique (title, clean_url) article links with event-like titles."""
    seen, out = set(), []
    for text, href in anchors:
        parts = urlsplit(href)
        if not ARTICLE_RE.match(parts.path):
            continue
        clean = urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
        title = " ".join(text.split())
        if clean in seen or not title:
            continue
        seen.add(clean)
        if EVENT_TITLE.search(title):
            out.append((title, clean))
    return out[:MAX_ARTICLES]


def parse_article_ld(ld_blobs: list[str]) -> tuple[str, str]:
    """Return (datePublished, body) from the NewsArticle JSON-LD block."""
    for blob in ld_blobs:
        try:
            data = json.loads(blob)
        except ValueError:
            continue
        for node in data if isinstance(data, list) else [data]:
            if isinstance(node, dict) and node.get("@type") in ("NewsArticle", "Article"):
                body = node.get("articleBody") or node.get("description") or ""
                return node.get("datePublished", ""), body
    return "", ""


def fetch(context, settings, today: date) -> list[Doc]:
    page = context.new_page()
    anchors: list = []
    for listing in LISTINGS:
        try:
            page.goto(listing, wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(2000)
            anchors += page.eval_on_selector_all("a[href]", "els => els.map(e => [e.innerText, e.href])")
        except Exception:
            continue  # one listing page failing must not stop the others
    docs: list[Doc] = []
    for title, url in parse_listing(anchors):
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
            blobs = page.eval_on_selector_all(
                'script[type="application/ld+json"]', "els => els.map(e => e.textContent)"
            )
            published_raw, body = parse_article_ld(blobs)
            if not body:
                body = page.inner_text("body")[:4000]
            published = parse_any_date(published_raw)
            if in_window(published, today, settings.lookback_days):
                docs.append(Doc(url=url, text=f"Title: {title}\n{body[:4000]}", published=published))
        except Exception:
            continue  # one bad article must not sink the source
    page.close()
    return docs
