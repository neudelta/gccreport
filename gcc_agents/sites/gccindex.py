"""GCC Index (gccindex.in) events - requires a login.

Credentials come from GCCINDEX_USERNAME / GCCINDEX_PASSWORD (.env) and are only ever typed into the site's own
login form. The session is persisted to data/gccindex_state.json so we log in only when it expires.

The events page loads its rows from its own JSON feed (/events/data) with exact announcement dates, event type,
city and sector. We open the page like a normal browser, let it make that request, and parse the JSON it
received (the page text only says "Sept 2026", so it must not be used for dates). We never call the feed
directly: the site refuses that ("Direct access not permitted"). We also ask the page for 20 rows per page
using its own dropdown. The raw capture is written to data/gccindex_debug.txt for troubleshooting.
"""
import json
from datetime import date
from typing import Optional

from .. import config
from ..models import GCC
from .base import USER_AGENT, Doc, SiteBlocked, SiteSkipped, in_window, parse_any_date

BASE = "https://gccindex.in"
EVENTS = f"{BASE}/events"
FEED_PATH = "/events/data"

_SECTOR = {
    "Financial Services": "BFSI",
    "Healthcare & Life Sciences": "Healthcare/Pharma",
    "Retail & Consumer": "Retail/CPG",
    "Food & Agriculture": "Retail/CPG",
    "Manufacturing & Industrial": "Manufacturing/Auto",
    "Aerospace & Defense": "Manufacturing/Auto",
    "Energy & Utilities": "Energy",
    "Technology & Software": "Technology",
    "Semiconductors & Electronics": "Technology",
    "Telecom & Media": "Technology",
    "Consulting & Professional Services": "Technology",
}


def parse_events(bodies: list[str], today: date, lookback_days: int) -> list[GCC]:
    """Turn the feed's JSON into GCC items. Rows up to ~4 months old are kept: evaluate rejects the ones
    outside the window, and uses them to recognise stale re-reports of an older announcement."""
    seen: set[str] = set()
    items: list[GCC] = []
    for body in bodies:
        try:
            rows = json.loads(body)
        except ValueError:
            continue
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict) or not row.get("companyName"):
                continue
            key = str(row.get("id") or f"{row['companyName']}|{row.get('announcementDate')}")
            if key in seen:
                continue
            seen.add(key)
            when: Optional[date] = parse_any_date(str(row.get("announcementDate") or ""))
            if when is None or not in_window(when, today, lookback_days + 120):
                continue
            kind = "new_gcc" if str(row.get("eventType", "")).upper() == "NEW_GCC" else "expansion"
            jobs = row.get("jobsCount")
            items.append(
                GCC(
                    company=str(row["companyName"]).strip(),
                    city=str(row.get("city") or "").strip(),
                    sector=_SECTOR.get(str(row.get("sectorNormalized") or ""), "Other"),
                    event_type=kind,
                    status="announced",  # the judge upgrades to "opened" when the text says so
                    event_date=when,
                    headcount=str(jobs) if jobs else "",
                    summary=str(row.get("eventContext") or "").strip(),
                    source_urls=[],  # the events page is login-walled, so it is not a link worth showing
                    discovery_sources=["gccindex"],
                )
            )
    return items


def _login(page, settings) -> None:
    page.goto(f"{BASE}/login", wait_until="domcontentloaded", timeout=30000)
    page.fill("#username", settings.gccindex_user)
    page.fill("#password", settings.gccindex_pass)
    page.check("#remember-me")
    page.click("#submitBtn")
    page.wait_for_load_state("networkidle", timeout=30000)
    if "/login" in page.url:
        raise SiteBlocked("GCC Index login failed (check GCCINDEX_USERNAME / GCCINDEX_PASSWORD)")


def _show_20_per_page(page) -> None:
    """Use the page's own '20 / page' dropdown so one load covers more than a week of events."""
    try:
        select = page.locator("select").filter(has_text="20 / page")
        if select.count():
            select.first.select_option(label="20 / page")
            page.wait_for_load_state("networkidle", timeout=15000)
    except Exception:
        pass  # the default 10 rows still work


def fetch(browser, settings, today: date) -> list[Doc]:
    if not (settings.gccindex_user and settings.gccindex_pass):
        raise SiteSkipped("GCCINDEX_USERNAME / GCCINDEX_PASSWORD not set")
    state = config.GCCINDEX_STATE
    context = browser.new_context(user_agent=USER_AGENT, storage_state=str(state) if state.exists() else None)
    page = context.new_page()
    bodies: list[str] = []

    def on_response(resp):
        try:
            if resp.url.startswith(BASE) and FEED_PATH in resp.url and resp.status == 200:
                bodies.append(resp.text())
        except Exception:
            pass

    page.on("response", on_response)
    page.goto(EVENTS, wait_until="networkidle", timeout=45000)
    if "/login" in page.url:
        _login(page, settings)
        state.parent.mkdir(parents=True, exist_ok=True)
        context.storage_state(path=str(state))
        bodies.clear()
        page.goto(EVENTS, wait_until="networkidle", timeout=45000)
    page.wait_for_timeout(1500)
    _show_20_per_page(page)
    page_text = page.inner_text("body")
    context.close()

    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    dump = f"URL: {EVENTS}\n{page_text}\n\n" + "\n\n".join(f"# {FEED_PATH}\n{b}" for b in bodies)
    (config.DATA_DIR / "gccindex_debug.txt").write_text(dump[:200000], encoding="utf-8")

    items = parse_events(bodies, today, settings.lookback_days)
    if not items and not bodies:
        raise SiteBlocked("GCC Index feed did not load (layout may have changed)")
    return [Doc(url=EVENTS, text="", items=items)]
