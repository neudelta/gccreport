"""Google Search (via Serper + LangChain) -> candidate GCC events."""
import warnings
from concurrent.futures import ThreadPoolExecutor
from datetime import date

from .config import Settings
from .llm import extract_items
from .dates import parse_web_date
from .models import GCC
from .tracing import map_in_context, trace_tool

warnings.filterwarnings("ignore", category=DeprecationWarning, module="langchain_community")

SITE_QUERIES = {
    "et_gcc": "site:gcc.economictimes.indiatimes.com GCC",
    "et_gcc_wt": "site:gcc.economictimes.indiatimes.com/news/workplace-transformation",
    "punegcc": "site:punegcc.com",
    "et_gov": "site:government.economictimes.indiatimes.com GCC",
    "gcc_pulse": "site:gcc-pulse.com GCC",
    "moneycontrol": "site:moneycontrol.com GCC India",
    "gccindex": "site:gccindex.in GCC",
}

BASE_QUERIES = [
    "GCC India opens new global capability centre",
    "global capability centre India inaugurates",
    "GCC expansion India new facility seats",
    "sets up global capability centre in India",
    "captive centre India launch announces",
    "GCC India launch press release",
    # state approvals / MoUs list several companies at once (e.g. UP cabinet clearing four GCCs)
    "cabinet approves global capability centre projects",
    "letter of comfort GCC investment",
    "GCC MoU signed state government India",
    # openings that are not called "GCC" in the headline
    "opens new office in India to house employees technology",
    "enters India tech hub Bengaluru OR Hyderabad OR Pune OR Chennai",
    "India technology centre inaugurated multinational",
    "new India campus engineering centre opens",
]

CITIES = [
    "Bengaluru", "Hyderabad", "Pune", "Chennai", "Gurugram", "Noida", "Mumbai", "Kolkata",
    "Ahmedabad", "Kochi", "Coimbatore", "Jaipur", "Indore", "Thiruvananthapuram", "Visakhapatnam",
]


def _wrapper(settings: Settings, kind: str):
    from langchain_community.utilities import GoogleSerperAPIWrapper

    return GoogleSerperAPIWrapper(
        serper_api_key=settings.serper_key, gl="in", hl="en", k=10, tbs=time_filter(settings), type=kind
    )


def time_filter(settings: Settings) -> str:
    """Google 'past N days' filter matching the report window (qdr:w alone silently dropped week 2)."""
    return f"qdr:d{max(1, settings.lookback_days)}"


@trace_tool("google_serper_search")
def _search(settings: Settings, query: str) -> list[dict]:
    hits: list[dict] = []
    for kind, key in (("search", "organic"), ("news", "news")):
        try:
            data = _wrapper(settings, kind).results(query)
        except Exception:
            continue
        hits.extend(data.get(key, []) or [])
    return hits


def queries_for(attempt: int) -> list[str]:
    if attempt == 0:
        return BASE_QUERIES + list(SITE_QUERIES.values())
    return [f"global capability centre {c} opens OR expands OR inaugurates" for c in CITIES]


def _format(hits: list[dict], today: date) -> list[str]:
    seen, lines = set(), []
    for h in hits:
        link = h.get("link", "")
        if not link or link in seen:
            continue
        seen.add(link)
        raw = h.get("date", "")
        published = parse_web_date(raw, today)
        stamp = published.isoformat() if published else (raw or "unknown")
        lines.append(
            f"- {h.get('title', '')} | {h.get('source', '')} | Published: {stamp}\n"
            f"  {h.get('snippet', '')}\n  {link}"
        )
    return lines


def run(settings: Settings, today: date, already_reported: list[str], attempt: int = 0) -> tuple[list[GCC], str]:
    """Returns (candidates, status). status is 'ok' / 'failed: <why>' / 'skipped: <why>'."""
    if not settings.serper_key:
        return [], "skipped: SERPER_API_KEY not set"
    try:
        with ThreadPoolExecutor(max_workers=6) as pool:
            batches = map_in_context(pool, lambda q: _search(settings, q), queries_for(attempt))
        lines = _format([h for b in batches for h in b], today)
        if not lines:
            return [], "ok: 0 results"
        items: list[GCC] = []
        for i in range(0, len(lines), 30):
            chunk = "\n".join(lines[i : i + 30])
            items += extract_items(settings, chunk, "serper", today=today, already_reported=already_reported)
        return items, f"ok: {len(lines)} results, {len(items)} candidates"
    except Exception as exc:  # isolate: one source failing must not kill the run
        return [], f"failed: {type(exc).__name__}: {exc}"[:300]
