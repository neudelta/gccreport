"""Runs the GCC news-site adapters (ET GCC, GCC Pulse, Moneycontrol, GCC Index) and extracts candidates.

Each site is isolated: a failure/block/skip in one is recorded in its own status and never affects the others.
"""
from datetime import date

from .config import Settings
from .llm import extract_items
from .models import GCC
from .sites import et_gcc, gcc_pulse, gccindex, moneycontrol
from .sites.base import USER_AGENT, SiteBlocked, SiteSkipped
from .tracing import trace_tool

ADAPTERS = {
    "et_gcc": et_gcc,
    "gcc_pulse": gcc_pulse,
    "moneycontrol": moneycontrol,
    "gccindex": gccindex,
}


@trace_tool(
    "scrape_site",
    process_inputs=lambda d: {"site": d["name"]},
    process_outputs=lambda docs: {"doc_count": len(docs), "urls": [x.url for x in docs][:15]} if docs else {},
)
def _scrape(name: str, adapter, target, settings: Settings, today: date) -> list:
    """Fetch one site's documents. `target` is the Playwright browser (gccindex) or a fresh context."""
    return adapter.fetch(target, settings, today)


def run(settings: Settings, today: date, already_reported: list[str]) -> tuple[list[GCC], dict[str, str]]:
    names = [n for n in ADAPTERS if settings.source_enabled(n)]
    status: dict[str, str] = {}
    items: list[GCC] = []
    if not names:
        return items, status

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return items, {n: "failed: playwright not installed" for n in names}

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        try:
            for name in names:
                adapter = ADAPTERS[name]
                context = None
                try:
                    if name == "gccindex":
                        docs = _scrape(name, adapter, browser, settings, today)  # manages its own context
                    else:
                        context = browser.new_context(user_agent=USER_AGENT, locale="en-IN")
                        docs = _scrape(name, adapter, context, settings, today)
                    found: list[GCC] = []
                    for doc in docs:  # one call per document, so each item inherits its own publish date
                        if doc.items is not None:  # structured source: already parsed, skip the LLM
                            found += doc.items
                            continue
                        text = f"URL: {doc.url}\nPublished: {doc.published or 'unknown'}\n{doc.text}"
                        for it in extract_items(settings, text, name, today=today,
                                                already_reported=already_reported, default_date=doc.published):
                            if doc.url not in it.source_urls:
                                it.source_urls.append(doc.url)
                            found.append(it)
                    items += found
                    status[name] = f"ok: {len(docs)} docs, {len(found)} candidates"
                except SiteSkipped as exc:
                    status[name] = f"skipped: {exc}"
                except SiteBlocked as exc:
                    status[name] = f"blocked: {exc} (covered via Serper site: search)"
                except Exception as exc:
                    status[name] = f"failed: {type(exc).__name__}: {exc}"[:300]
                finally:
                    if context:
                        context.close()
        finally:
            browser.close()
    return items, status
