"""Evaluate candidates against explicit success criteria.

A candidate is approved only if ALL hold:
  1. in scope (new GCC / expansion, opened or - if INCLUDE_ANNOUNCED - officially announced),
  2. event date is inside the lookback window,
  3. it is genuinely a GCC/captive centre in India (LLM judge),
  4. it has not been reported before (DB) and is not a duplicate within this run,
  5. it has at least one source link. The best available is shown: a verified official press release if one
     exists, otherwise the company's own post, otherwise a news article. A missing official release does NOT
     reject an item.
"""
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from typing import Literal, Optional
from urllib.parse import urlsplit

import httpx
from bs4 import BeautifulSoup
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from . import db
from .config import Settings
from .llm import chat
from .models import GCC, SECTORS, canonical_url
from .tracing import map_in_context, trace_tool

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
PR_WIRES = (
    "prnewswire.com", "businesswire.com", "globenewswire.com", "newswire.ca", "prnewswire.co.uk",
    "accesswire.com", "pib.gov.in", "investindia.gov.in", "ptinews.com",
)
# Hosts that can never count as a company's own newsroom, even if the company name appears in the URL.
NOT_OFFICIAL_HOSTS = (
    "linkedin.com", "facebook.com", "instagram.com", "x.com", "twitter.com", "youtube.com", "glassdoor",
    "ambitionbox", "naukri", "indeed.", "randstad", "apna.co", "bebee.com", "wikipedia.org", "medium.com",
)
_NON_NEWS_PATH = re.compile(
    r"/(careers?|jobs?|vacanc[\w-]*|contact[\w-]*|lets-talk|get-in-touch|about[\w-]*|locations?|offices?|"
    r"privacy|terms|login|search|investors?)(/|$)", re.I
)
BLOCKED_NOTE = "official domain (page blocked automated fetch)"
_NEWS_PATH = re.compile(r"(news|press|media|release|stor(y|ies)|blog|article|insight|announce)", re.I)
_EVENT_WORDS = re.compile(
    r"(gcc|capability cent|centre|center|office|hub|campus|facility|inaugurat|opens?\b|opened|launch)", re.I
)
SOURCE_PRIORITY = ["et_gcc", "gcc_pulse", "serper", "gccindex", "groq_x", "moneycontrol"]
MAX_RESOLVE_PER_RUN = 20


class Verdict(BaseModel):
    in_scope: bool = Field(description="True only for a genuine GCC/captive centre event in India (see rules)")
    event_type: Literal["new_gcc", "expansion"]
    status: Literal["opened", "announced"]
    sector: str = Field(description=f"One of: {', '.join(SECTORS)}")
    same_as_prior: bool = Field(description="True if this is the same event as one of the prior records listed")
    reason: str = Field(description="One short sentence justifying the decision")


JUDGE_SYSTEM = f"""You are a strict analyst deciding whether a candidate is a qualifying India GCC event.
Qualifies: a company (usually a multinational) sets up / launches / opens a Global Capability Centre (captive
centre) in India = new_gcc; or an existing India GCC opens a new centre/facility/city or announces a major
seat/headcount expansion = expansion.
Does NOT qualify: rumours, "in talks", hiring-only news, leadership appointments, layoffs/restructuring,
acquisitions, events/conferences, Indian IT-services firms' ordinary offices, opinion/trend pieces, centres
outside India.
Also set event_type/status to what the evidence supports, normalise sector to one of: {', '.join(SECTORS)},
and set same_as_prior=true if the candidate describes the same event as one of the listed prior records."""


# ---------- pure helpers (unit-tested) ----------

def _host(url: str) -> str:
    return urlsplit(url).netloc.lower().removeprefix("www.")


def company_token(company_key: str) -> str:
    tokens = [t for t in company_key.split() if len(t) >= 3]
    return tokens[0] if tokens else company_key.replace(" ", "")


def host_matches_company(host: str, company_key: str) -> bool:
    """The domain itself carries the company's name (woodside.com, news.acme.com)."""
    flat = host.replace("-", "").replace(".", "")
    joined = company_key.replace(" ", "")
    token = company_token(company_key)
    return bool(joined and len(joined) >= 4 and joined in flat) or (len(token) >= 4 and token in flat)


def is_pr_wire_or_gov(host: str) -> bool:
    return (
        any(host == w or host.endswith("." + w) for w in PR_WIRES)
        or host.endswith((".gov.in", ".nic.in", ".gov"))
    )


def is_official_domain(url: str, company_key: str) -> bool:
    host = _host(url)
    if not host:
        return False
    if any(s in host for s in NOT_OFFICIAL_HOSTS):
        return False
    return is_pr_wire_or_gov(host) or host_matches_company(host, company_key)


def merge_candidates(cands: list[GCC]) -> list[GCC]:
    """Merge cross-source duplicates (same company+city, dates within 14 days) into one item."""
    def rank(c: GCC) -> int:
        srcs = c.discovery_sources
        return min((SOURCE_PRIORITY.index(s) for s in srcs if s in SOURCE_PRIORITY), default=99)

    merged: list[GCC] = []
    for c in sorted(cands, key=rank):
        target = None
        for m in merged:
            if m.company_key == c.company_key and m.city_key == c.city_key:
                if m.event_date is None or c.event_date is None or abs((m.event_date - c.event_date).days) <= 14:
                    target = m
                    break
        if target is None:
            merged.append(c.model_copy(deep=True))
            continue
        target.discovery_sources = list(dict.fromkeys(target.discovery_sources + c.discovery_sources))
        target.source_urls = list(dict.fromkeys(target.source_urls + c.source_urls))
        target.press_release_url = target.press_release_url or c.press_release_url
        if c.status == "opened":
            target.status = "opened"
        target.event_date = target.event_date or c.event_date
        target.summary = target.summary or c.summary
    return merged


def precheck(item: GCC, today: date, settings: Settings) -> Optional[str]:
    """Deterministic gate. Returns a rejection reason, or None if it may proceed."""
    if not item.company:
        return "no company name"
    if not item.city_key:
        return "no city stated"
    if item.event_date is None:
        return "no event date"
    if not (today - timedelta(days=settings.lookback_days) <= item.event_date <= today + timedelta(days=1)):
        return f"event date {item.event_date} outside the {settings.lookback_days}-day window"
    if item.status != "opened" and not settings.include_announced:
        return "only announced (INCLUDE_ANNOUNCED=false)"
    return None


# ---------- network helpers ----------

_LISTING_SLUGS = {
    "news", "newsroom", "announcements", "press", "press-releases", "press-release", "media", "media-centre",
    "media-center", "releases", "blog", "stories", "insights", "articles", "latest",
}


def looks_like_article(path: str) -> bool:
    """A specific post (…/woodside-opens-bengaluru-centre), not an index like /media-centre/announcements."""
    segments = [s for s in path.split("/") if s]
    if not segments:
        return False
    last = segments[-1].rsplit(".", 1)[0].lower()
    if last in _LISTING_SLUGS:
        return False
    return last.count("-") >= 3 or bool(re.search(r"\d{5,}", last)) or (
        len(segments) >= 3 and bool(_NEWS_PATH.search(path)) and last.count("-") >= 2
    )


@trace_tool("verify_press_release_url")
def verify_url(url: str, company_key: str, city_key: str = "") -> tuple[bool, str]:
    """An official-domain page that is really ABOUT the event (not a careers/contact page).

    Passes only if: official domain, not a careers/contact-type path, fetchable, mentions the company, mentions
    the city (or India) and reads like a centre/office/launch announcement.
    """
    if not url or not is_official_domain(url, company_key):
        return False, "not an official domain"
    path = urlsplit(url).path
    if _NON_NEWS_PATH.search(path):
        return False, "careers/contact-type page, not an announcement"
    try:
        resp = httpx.get(url, headers={"User-Agent": USER_AGENT}, follow_redirects=True, timeout=15)
    except httpx.HTTPError as exc:
        return False, f"fetch error: {type(exc).__name__}"
    if resp.status_code in (401, 403, 429):
        # Many corporate sites block scripts; trust the URL only if it clearly looks like a news/press page.
        return (is_pr_wire_or_gov(_host(url)) or looks_like_article(path)), BLOCKED_NOTE
    if resp.status_code != 200:
        return False, f"HTTP {resp.status_code}"
    text = _page_text(resp.text)
    lowered = text.casefold()
    token = company_token(company_key)
    if token and token not in lowered:
        return False, "page does not mention the company"
    if not ((city_key and city_key in lowered) or "india" in lowered):
        return False, "page does not mention the city or India"
    if not _EVENT_WORDS.search(text):
        return False, "page does not read like a centre/office announcement"
    return True, text[:4000]


def _page_text(html: str) -> str:
    """Visible text of a page without scripts, styles and site chrome."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "nav", "footer", "header", "svg"]):
        tag.decompose()
    return " ".join(soup.get_text(" ", strip=True).split())


class PageCheck(BaseModel):
    announces_event: bool = Field(description="True only if THIS page itself reports the specific event")
    reason: str = Field(description="One short sentence")


@trace_tool("confirm_announcement")
def confirm_announcement(settings: Settings, item: GCC, page_text: str) -> bool:
    """LLM check that the page really is the announcement of this event, not just a page on the same site."""
    what = "opening/launching a GCC (captive centre)" if item.event_type == "new_gcc" else "expanding its GCC/centre"
    prompt = (
        f"Event: {item.company} {what} in {item.city}, India (around {item.event_date}).\n"
        f"Summary: {item.summary}\n\n--- PAGE TEXT ---\n{page_text[:4000]}"
    )
    system = (
        "Decide whether this web page is itself an announcement/press release/news post about the event above. "
        "Answer false for generic index or listing pages, contact, careers, about or locations pages, annual "
        "reports, and pages about other topics or other cities."
    )
    try:
        verdict = chat(settings, judge=True).with_structured_output(PageCheck).invoke(
            [SystemMessage(content=system), HumanMessage(content=prompt)]
        )
        return verdict.announces_event
    except Exception:
        return False


def accept_source(settings: Settings, item: GCC, url: str) -> Optional[str]:
    """Canonical URL if `url` is an official page that announces this event, else None."""
    ok, page = verify_url(url, item.company_key, item.city_key)
    if not ok:
        return None
    if page == BLOCKED_NOTE:  # can't read it; the domain and news-like path were already vetted
        return canonical_url(url)
    return canonical_url(url) if confirm_announcement(settings, item, page) else None


def _serper_organic(settings: Settings, query: str, tbs: Optional[str] = None) -> list[dict]:
    from langchain_community.utilities import GoogleSerperAPIWrapper

    kwargs = {"serper_api_key": settings.serper_key, "gl": "in", "hl": "en", "k": 10}
    if tbs:
        kwargs["tbs"] = tbs
    try:
        return GoogleSerperAPIWrapper(**kwargs).results(query).get("organic", []) or []
    except Exception:
        return []


def _company_domain(settings: Settings, item: GCC) -> Optional[str]:
    """The company's own website host, found by searching for its official site."""
    for hit in _serper_organic(settings, f'"{item.company}" official website'):
        host = _host(hit.get("link", ""))
        if host and not any(s in host for s in NOT_OFFICIAL_HOSTS) and host_matches_company(host, item.company_key):
            return host
    return None


SOCIAL_HOSTS = ("linkedin.com", "x.com", "twitter.com", "facebook.com", "instagram.com")
JUNK_HOSTS = (
    "glassdoor", "ambitionbox", "naukri", "indeed.", "randstad", "apna.co", "bebee.com", "myinternships",
    "greatplacetowork", "wikipedia.org", "monster", "shine.com", "foundit", "jobs.",
    "gccindex.in",  # login-walled, useless as a link for the reader
)


def is_company_post(url: str, company_key: str) -> bool:
    """A post on the company's OWN LinkedIn/X account (the account slug carries the company name)."""
    parts = urlsplit(url)
    host = parts.netloc.lower().removeprefix("www.")
    if not any(host == h or host.endswith("." + h) for h in SOCIAL_HOSTS):
        return False
    joined, token = company_key.replace(" ", ""), company_token(company_key)
    if host.endswith("linkedin.com"):
        # /posts/<account>_<post-text>… or /company/<account>/… ; the account is the segment before the first "_"
        m = re.match(r"/(?:posts|company)/([^/_]+)", parts.path.lower())
        account = m.group(1).replace("-", "") if m else ""
    else:  # x.com/<handle>/status/<id>
        account = parts.path.lower().strip("/").split("/")[0].replace("_", "")
    return bool(account) and (
        (len(joined) >= 4 and joined in account) or (len(token) >= 4 and token in account)
    )


def fallback_candidates(item: GCC, hits: list[dict]) -> list[tuple[str, str, str]]:
    """Non-official links worth trying, best first, as (url, kind, blurb). Pure: does no network calls.

    Order: the company's own post, then articles that reported the event, then search hits about the company.
    """
    key = item.company_key
    token = company_token(key)
    # The articles that reported the event come first; search hits are only used if those don't help.
    urls = [(u, "") for u in item.source_urls]
    urls += [(h.get("link", ""), f"{h.get('title', '')} {h.get('snippet', '')}") for h in hits]
    out: list[tuple[str, str, str]] = []
    seen: set[str] = set()

    def add(link: str, kind: str, blurb: str) -> None:
        if link and link not in seen:
            seen.add(link)
            out.append((link, kind, blurb))

    for link, blurb in urls:  # 1. the company's own post
        if link and is_company_post(link, key):
            add(link, "company_post", blurb)
    for link, blurb in urls:  # 2. news articles: reporting sources first, then hits that mention the company
        host = _host(link)
        if not link or not host or any(s in host for s in JUNK_HOSTS) or any(s in host for s in SOCIAL_HOSTS):
            continue
        if looks_like_article(urlsplit(link).path) and (link in item.source_urls or token in blurb.casefold()):
            add(link, "news", blurb)
    for link, blurb in urls:  # 3. anything else that is not junk and mentions the company
        host = _host(link)
        if link and host and not any(s in host for s in JUNK_HOSTS) and (token in blurb.casefold() or not blurb):
            add(link, "post" if any(s in host for s in SOCIAL_HOSTS) else "news", blurb)
    return out


def news_is_relevant(settings: Settings, item: GCC, url: str, blurb: str = "") -> bool:
    """Is this page really about this event? Fetch it and ask the LLM; if it can't be read, fall back to hints."""
    token, city = company_token(item.company_key), item.city_key
    try:
        resp = httpx.get(url, headers={"User-Agent": USER_AGENT}, follow_redirects=True, timeout=15)
        if resp.status_code == 200:
            return confirm_announcement(settings, item, _page_text(resp.text))
    except httpx.HTTPError:
        pass
    hint = f"{url} {blurb}".casefold()  # unreadable page (paywall/bot block): accept only on strong text hints
    return token in hint and (city in hint or "gcc" in hint or "capability" in hint)


def choose_fallback(settings: Settings, item: GCC, hits: list[dict], max_news_checks: int = 4) -> tuple[str, str]:
    """Best non-official source: a company post as-is, otherwise the first news link that is verified relevant."""
    checked = 0
    for url, kind, blurb in fallback_candidates(item, hits):
        if kind == "company_post":
            return url, kind
        if checked >= max_news_checks:
            break
        checked += 1
        if news_is_relevant(settings, item, url, blurb):
            return url, kind
    return "", ""


@trace_tool("find_press_release")
def find_press_release(settings: Settings, item: GCC, fallbacks: Optional[list] = None) -> Optional[str]:
    """Look for an official announcement of this event, most authoritative places first.

    1. general Google search, 2. the PR wires, 3. the company's own website (site: search).
    Every candidate link must pass verify_url and an LLM check. All search hits seen are appended to
    `fallbacks` so the caller can fall back to a company post or news article.
    """
    if not settings.serper_key:
        return None
    kind = "GCC" if item.event_type == "new_gcc" else "GCC expansion"
    tried: set[str] = set()
    fallbacks = fallbacks if fallbacks is not None else []

    def first_verified(hits: list[dict]) -> Optional[str]:
        fallbacks.extend(hits)
        for hit in hits:
            link = hit.get("link", "")
            if not link or link in tried:
                continue
            tried.add(link)
            accepted = accept_source(settings, item, link)
            if accepted:
                return accepted
        return None

    wires = "site:prnewswire.com OR site:businesswire.com OR site:globenewswire.com"
    for query, tbs in (
        (f'"{item.company}" {item.city} {kind} India press release', "qdr:m"),
        (f'"{item.company}" India {item.city} centre ({wires})', "qdr:m"),
    ):
        found = first_verified(_serper_organic(settings, query, tbs))
        if found:
            return found

    domain = _company_domain(settings, item)
    if domain:
        return first_verified(
            _serper_organic(settings, f'site:{domain} {item.city} India (GCC OR centre OR center OR office OR hub)')
        )
    return None


# ---------- the node ----------

def _judge(settings: Settings, item: GCC, prior: list[dict]) -> Verdict:
    llm = chat(settings, judge=True).with_structured_output(Verdict)
    prompt = (
        f"Candidate: {item.model_dump_json(exclude={'discovery_sources'})}\n"
        f"Found via: {', '.join(item.discovery_sources)}\n"
        f"Prior recorded events for this company: {prior or 'none'}"
    )
    return llm.invoke([SystemMessage(content=JUDGE_SYSTEM), HumanMessage(content=prompt)])


def _evaluate_one(settings: Settings, item: GCC, prior: list[dict], resolve_budget: dict) -> tuple[Optional[GCC], str]:
    try:
        verdict = _judge(settings, item, prior)
    except Exception as exc:
        return None, f"judge error: {type(exc).__name__}"
    if not verdict.in_scope:
        return None, f"not a qualifying GCC event: {verdict.reason}"
    if verdict.same_as_prior:
        return None, "same event as a previously reported one"
    item = item.model_copy(
        update={
            "event_type": verdict.event_type,
            "status": verdict.status,
            "sector": verdict.sector if verdict.sector in SECTORS else "Other",
        }
    )
    if item.status != "opened" and not settings.include_announced:
        return None, "only announced (INCLUDE_ANNOUNCED=false)"

    # Pick the best link to show. Prefer a verified official press release, but never reject for lacking one:
    # fall back to the company's own post, then a news article about it.
    url = accept_source(settings, item, item.press_release_url) if item.press_release_url else None
    hits: list[dict] = []
    if not url and resolve_budget["left"] > 0:
        resolve_budget["left"] -= 1
        url = find_press_release(settings, item, hits)
    if url:
        kind = "press_release"
    else:
        url, kind = choose_fallback(settings, item, hits)
    if not url:
        return None, "no source link found"
    return item.model_copy(
        update={
            "source_url": url,
            "source_kind": kind,
            "press_release_url": url if kind == "press_release" else "",
        }
    ), "approved"


def run(settings: Settings, conn, candidates: list[GCC], today: date) -> tuple[list[GCC], list[dict], int]:
    """Returns (approved, rejected, already_reported_count) for the given (new) candidates."""
    approved: list[GCC] = []
    rejected: list[dict] = []
    duplicates = 0

    # A "new GCC" is new once. If any candidate (e.g. from GCC Index) shows the same company+city announced
    # much earlier, a later article about it is stale news, not a fresh event.
    first_seen: dict[tuple[str, str], date] = {}
    for c in candidates:
        if c.event_type == "new_gcc" and c.event_date:
            k = (c.company_key, c.city_key)
            first_seen[k] = min(first_seen.get(k, c.event_date), c.event_date)

    pending: list[tuple[GCC, list[dict]]] = []
    for item in merge_candidates(candidates):
        reason = precheck(item, today, settings)
        earlier = first_seen.get((item.company_key, item.city_key))
        if (not reason and item.event_type == "new_gcc" and earlier and item.event_date
                and (item.event_date - earlier).days > 14):
            reason = f"stale: this GCC was already announced on {earlier}"
        if reason:
            rejected.append(_rej(item, reason))
        elif db.is_duplicate(conn, item):
            duplicates += 1
        else:
            pending.append((item, db.prior_for_company(conn, item.company)))

    budget = {"left": MAX_RESOLVE_PER_RUN}
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = map_in_context(pool, lambda p: _evaluate_one(settings, p[0], p[1], budget), pending)

    for (orig, _), (final, reason) in zip(pending, results):
        if final is None:
            rejected.append(_rej(orig, reason))
        elif db.is_duplicate(conn, final) or any(a.event_key == final.event_key for a in approved):
            duplicates += 1
        else:
            approved.append(final)
    return approved, rejected, duplicates


def _rej(item: GCC, reason: str) -> dict:
    return {
        "company": item.company, "city": item.city, "event_type": item.event_type,
        "sources": item.discovery_sources, "reason": reason,
    }
