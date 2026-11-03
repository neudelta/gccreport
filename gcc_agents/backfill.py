"""Build the 2026 (or any year's) GCC catalogue in the local database, for the dashboard.

Sources:
  1. GCC Index - the events feed your logged-in session already receives (exact dates, type, city, sector).
  2. Google via Serper, one calendar month at a time, read by the same OpenAI extraction as the weekly run.

The same quality rules as the weekly run apply (real company, city stated, no IT-services competitors, merged
duplicates). Google-only items also go through the AI judge; GCC Index items are already validated by GCC Index.
No LTM analysis is generated here (that is on demand in the dashboard).

Items are stored with origin='backfill'. Backfill stops at the end of the last emailed week, so anything newer
still goes out in the next weekly email instead of being treated as already reported.
"""
import calendar
import csv
import json
import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date
from typing import Optional

from . import config, db, evaluate_agent
from .config import Settings
from .dates import parse_web_date
from .llm import extract_items
from .models import GCC, SECTORS
from .serper_source import _format
from .tracing import map_in_context

log = logging.getLogger("gcc")

MONTH_QUERIES = [
    "global capability centre India opens",
    "GCC India inaugurates new centre",
    "sets up global capability centre in India",
    "GCC expansion India new facility",
    "new technology centre India multinational opens",
    "cabinet approves global capability centre projects",
    "GCC MoU letter of comfort India",
    "enters India tech hub Bengaluru OR Hyderabad OR Pune OR Chennai",
    "site:gcc.economictimes.indiatimes.com/news/workplace-transformation",
    "site:gcc-pulse.com GCC",
]


@dataclass
class BackfillResult:
    start: date
    end: date
    gccindex_rows: int = 0
    skipped: list[str] = field(default_factory=list)  # sources that did not run, and why
    google_candidates: int = 0
    kept: list[GCC] = field(default_factory=list)
    rejected: list[dict] = field(default_factory=list)
    added: int = 0


def months(start: date, end: date) -> list[tuple[date, date]]:
    """[(first_day, last_day)] for every calendar month touching [start, end], clipped to the range."""
    out, y, m = [], start.year, start.month
    while (y, m) <= (end.year, end.month):
        first = max(date(y, m, 1), start)
        last = min(date(y, m, calendar.monthrange(y, m)[1]), end)
        out.append((first, last))
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def month_filter(first: date, last: date) -> str:
    """Google custom date range (tbs) for one month."""
    return f"cdr:1,cd_min:{first.month}/{first.day}/{first.year},cd_max:{last.month}/{last.day}/{last.year}"


def default_end(conn, today: date) -> date:
    """End of the last emailed week, so newer GCCs are still sent in the next weekly email."""
    row = conn.execute("SELECT max(window_end) AS e FROM reports WHERE email_status = 'sent'").fetchone()
    return min(today, date.fromisoformat(row["e"])) if row and row["e"] else today


def catalog_reason(item: GCC, start: date, end: date) -> Optional[str]:
    """Deterministic rules for the catalogue (the weekly precheck, with a fixed date range)."""
    if not item.company:
        return "no company name"
    if item.company_key in evaluate_agent.IT_SERVICES_PROVIDERS:
        return "IT-services provider (competitor), not a captive GCC"
    if evaluate_agent.is_generic_name(item.company_key):
        return "not a company name (generic words only)"
    if not item.city_key or item.city_key.replace("/", " ").replace(".", "") in evaluate_agent._PLACEHOLDER_CITIES:
        return "no city stated"
    if item.event_date is None:
        return "no event date"
    if not (start <= item.event_date <= end):
        return f"event date {item.event_date} outside {start} - {end}"
    return None


def gccindex_items(settings: Settings, start: date, end: date) -> list[GCC]:
    if not (settings.gccindex_user and settings.gccindex_pass):
        log.warning("backfill: GCC Index credentials not set, skipping it")
        return []
    from playwright.sync_api import sync_playwright

    from .sites import gccindex

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        try:
            bodies = gccindex.capture_feed(browser, settings)
        finally:
            browser.close()
    return gccindex.parse_rows(bodies, start, end)


def google_month(settings: Settings, first: date, last: date) -> list[GCC]:
    from langchain_community.utilities import GoogleSerperAPIWrapper

    def one(query: str) -> list[dict]:
        hits: list[dict] = []
        for kind, key in (("search", "organic"), ("news", "news")):
            try:
                w = GoogleSerperAPIWrapper(serper_api_key=settings.serper_key, gl="in", hl="en", k=10,
                                           tbs=month_filter(first, last), type=kind)
                hits += w.results(query).get(key, []) or []
            except Exception:
                continue
        return hits

    with ThreadPoolExecutor(max_workers=6) as pool:
        batches = map_in_context(pool, one, MONTH_QUERIES)
    lines = _format(dated_hits([h for b in batches for h in b], first, last), last)
    items: list[GCC] = []
    for i in range(0, len(lines), 30):
        items += extract_items(settings, "\n".join(lines[i:i + 30]), "serper", today=last)
    return items


def dated_hits(hits: list[dict], first: date, last: date) -> list[dict]:
    """Only results whose own date falls inside the month. Google's date filter is loose for undated pages, and
    an undated hit made the AI guess a month-end date (Kuehne+Nagel showed as 29 Jan instead of 1 Oct)."""
    out = []
    for h in hits:
        d = parse_web_date(h.get("date", ""), last)
        if d and first <= d <= last:
            out.append(h)
    return out


def consolidate_new_gccs(items: list[GCC]) -> list[GCC]:
    """A company opens a new GCC in a city once: keep one row per (company, city) for new GCCs.

    Preference: the GCC Index row (exact announcement date), else the earliest date. Links and sources from the
    other rows are folded in. Expansions are left alone (a company can expand more than once a year).
    """
    from .evaluate_agent import same_company, same_group

    keep: list[GCC] = []
    for item in sorted(items, key=lambda i: ("gccindex" not in i.discovery_sources, i.event_date or date.max)):
        if item.event_type != "new_gcc":
            keep.append(item)
            continue
        twin = next((k for k in keep if k.event_type == "new_gcc" and k.city_key == item.city_key
                     and (same_company(k.company_key, item.company_key) or same_group(k.company_key, item.company_key))),
                    None)
        if twin is None:
            keep.append(item)
            continue
        twin.discovery_sources = list(dict.fromkeys(twin.discovery_sources + item.discovery_sources))
        twin.source_urls = list(dict.fromkeys(twin.source_urls + item.source_urls))
        twin.summary = twin.summary or item.summary
        twin.headcount = twin.headcount or item.headcount
    return keep


def _judge_google_only(settings: Settings, items: list[GCC]) -> tuple[list[GCC], list[dict]]:
    """AI judge for items no structured source confirmed (GCC Index rows are already validated)."""
    keep, rejected = [], []

    def one(item: GCC):
        if "gccindex" in item.discovery_sources:
            return item, None
        try:
            v = evaluate_agent._judge(settings, item, [])
        except Exception as exc:
            return None, f"judge error: {type(exc).__name__}"
        if not v.in_scope:
            return None, f"not a qualifying GCC event: {v.reason}"
        return item.model_copy(update={"event_type": v.event_type, "status": v.status,
                                       "sector": v.sector if v.sector in SECTORS else "Other"}), None

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = map_in_context(pool, one, items)
    for orig, (item, reason) in zip(items, results):
        if item is None:
            rejected.append({"company": orig.company, "city": orig.city, "reason": reason})
        else:
            keep.append(item)
    return keep, rejected


def _pick_link(item: GCC) -> GCC:
    """Cheapest reasonable link: the company's own post, else the article that reported it. No network calls."""
    if item.source_url:
        return item
    cands = evaluate_agent.fallback_candidates(item, [])
    if not cands:
        return item
    url, kind, _ = cands[0]
    return item.model_copy(update={"source_url": url, "source_kind": kind})


def run(settings: Settings, year: int, today: Optional[date] = None, dry_run: bool = False,
        use_google: bool = True) -> BackfillResult:
    today = today or date.today()
    start = date(year, 1, 1)
    with db.connect() as conn:
        end = min(date(year, 12, 31), default_end(conn, today))
    res = BackfillResult(start=start, end=end)
    log.info("backfill %d: %s to %s", year, start, end)

    candidates = gccindex_items(settings, start, end)
    res.gccindex_rows = len(candidates)
    log.info("backfill: GCC Index gave %d rows", len(candidates))

    if not settings.gccindex_user or not settings.gccindex_pass:
        res.skipped.append("GCC Index (GCCINDEX_USERNAME / GCCINDEX_PASSWORD not set in .env)")
    if use_google and not settings.serper_key:
        res.skipped.append("Google (SERPER_API_KEY not set in .env)")
        log.warning("backfill: SERPER_API_KEY is not set - skipping the Google search, results will be incomplete")
    if use_google and settings.serper_key:
        for first, last in months(start, end):
            found = google_month(settings, first, last)
            res.google_candidates += len(found)
            candidates += found
            log.info("backfill: Google %s gave %d candidates", first.strftime("%b %Y"), len(found))

    ok: list[GCC] = []
    for item in consolidate_new_gccs(evaluate_agent.merge_candidates(candidates)):
        reason = catalog_reason(item, start, end)
        if reason:
            res.rejected.append({"company": item.company, "city": item.city, "reason": reason})
        else:
            ok.append(item)
    judged, rejected = _judge_google_only(settings, ok)
    res.rejected += rejected
    # earliest first, so a GCC reported again later in the year keeps its first date (event_key is unique)
    res.kept = sorted((_pick_link(i) for i in judged), key=lambda i: i.event_date)

    if dry_run:
        _write_preview(res)
    else:
        with db.connect() as conn:
            res.added = db.save_catalog(conn, res.kept)
        reclassify_sectors(settings)
    log.info("backfill: %d kept, %d rejected, %d added to the database%s", len(res.kept), len(res.rejected),
             res.added, " (dry run)" if dry_run else "")
    return res


def _write_preview(res: BackfillResult) -> None:
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    path = config.DATA_DIR / "backfill_preview.csv"
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["event_date", "company", "city", "sector", "event_type", "status", "headcount", "sources",
                    "source_url", "summary"])
        for i in res.kept:
            w.writerow([i.event_date, i.company, i.city, i.sector, i.event_type, i.status, i.headcount,
                        "+".join(i.discovery_sources), i.source_url, i.summary])


# ---------------------------------------------------------------- sector clean-up
SECTOR_GUIDE = (
    "BFSI = banks, insurers, asset/wealth managers, private equity, payments, fintech, exchanges. "
    "Technology = software, SaaS, internet, semiconductors, telecom, IT products. "
    "Healthcare/Pharma = pharma, biotech, medtech, healthcare services. "
    "Retail/CPG = retail, e-commerce, consumer goods, food & beverage, restaurants. "
    "Manufacturing/Auto = industrial, automotive, aerospace, electrical/electronics hardware, chemicals, engineering. "
    "Energy = oil & gas, utilities, renewables. Other = only when none fits (professional services, consulting, "
    "logistics, media, real estate)."
)


def reclassify_sectors(settings: Settings, limit: int = 400) -> int:
    """Re-classify stored rows whose sector is 'Other' (old extraction was loose). Returns rows changed."""
    from typing import Literal

    from langchain_core.messages import HumanMessage, SystemMessage
    from pydantic import BaseModel

    from .llm import chat

    class One(BaseModel):
        id: int
        sector: Literal["BFSI", "Technology", "Healthcare/Pharma", "Retail/CPG", "Manufacturing/Auto", "Energy",
                        "Other"]

    class Many(BaseModel):
        rows: list[One]

    with db.connect() as conn:
        rows = conn.execute("SELECT id, company, summary, insight FROM gcc WHERE sector = 'Other' LIMIT ?",
                            (limit,)).fetchall()
    changed = 0
    llm = chat(settings).with_structured_output(Many)
    for i in range(0, len(rows), 40):
        batch = rows[i:i + 40]
        lines = []
        for r in batch:
            area = ""
            if r["insight"]:
                try:
                    area = json.loads(r["insight"]).get("business_area", "")
                except ValueError:
                    pass
            lines.append(f"{r['id']} | {r['company']} | {area or ''} | {(r['summary'] or '')[:200]}")
        try:
            out = llm.invoke([SystemMessage(content="Classify each company's industry sector. " + SECTOR_GUIDE
                                            + " Answer for every id given."),
                              HumanMessage(content="id | company | business | summary\n" + "\n".join(lines))])
        except Exception as exc:
            log.warning("sector clean-up batch failed: %s", type(exc).__name__)
            continue
        valid = {r["id"] for r in batch}
        with db.connect() as conn:
            for o in out.rows:
                if o.id in valid and o.sector != "Other":
                    changed += conn.execute("UPDATE gcc SET sector = ? WHERE id = ?", (o.sector, o.id)).rowcount
    log.info("sector clean-up: %d of %d 'Other' rows re-classified", changed, len(rows))
    return changed
