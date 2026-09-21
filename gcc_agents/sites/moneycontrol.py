"""Moneycontrol GCC tag page.

Moneycontrol sits behind bot protection ("Access Denied" for headless browsers). We do not try to evade it:
if blocked, the adapter raises SiteBlocked and coverage of this domain comes from the Serper
`site:moneycontrol.com` query instead.
"""
import re
from datetime import date

from .base import EVENT_TITLE, Doc, SiteBlocked

LISTING = "https://www.moneycontrol.com/news/tags/gcc.html"
ARTICLE_RE = re.compile(r"moneycontrol\.com/news/.+-\d{6,}\.html")


def fetch(context, settings, today: date) -> list[Doc]:
    page = context.new_page()
    page.goto(LISTING, wait_until="domcontentloaded", timeout=45000)
    page.wait_for_timeout(2000)
    if "access denied" in page.title().lower():
        page.close()
        raise SiteBlocked("Access Denied (bot protection)")
    anchors = page.eval_on_selector_all("a[href]", "els => els.map(e => [e.innerText, e.href])")
    docs, seen = [], set()
    for text, href in anchors:
        title = " ".join(text.split())
        if not ARTICLE_RE.search(href) or href in seen or not EVENT_TITLE.search(title):
            continue
        seen.add(href)
        try:
            page.goto(href, wait_until="domcontentloaded", timeout=30000)
            body = page.inner_text("article, .article_wrapper, body")[:4000]
            docs.append(Doc(url=href, text=f"Title: {title}\n{body}"))
        except Exception:
            continue
        if len(docs) >= 20:
            break
    page.close()
    return docs
