"""Insights agent: turns each approved GCC into account intelligence for LTM leadership.

For every company it gathers context (the event summary, the source page, and a company-profile search), then asks
the LLM to fill an `LTMInsight`: business area, products, what the India centre will do, and how LTM GCC Origin
can win business there (fit, services, engagement model, talking points, next step).

Facts about the company must come from the gathered context; the opportunity part is analysis, restricted to the
GCC Origin catalogue in `ltm.py`. If anything fails, the item still goes into the report without an insight.
"""
import logging
from concurrent.futures import ThreadPoolExecutor

import httpx
from langchain_core.messages import HumanMessage, SystemMessage

from . import ltm
from .config import Settings
from .evaluate_agent import USER_AGENT, _page_text, _serper_organic
from .llm import chat
from .models import GCC, LTMInsight
from .tracing import map_in_context, trace_tool

log = logging.getLogger("gcc")

INSIGHT_SYSTEM = f"""You are a strategy analyst in LTM's {ltm.BU_NAME} business unit, briefing LTM executives.
For one Global Capability Centre (GCC) event in India, fill in the account brief.

COMPANY FACTS (headquarters, business_area, products, gcc_mandate, scale):
- Use ONLY the context provided. Never invent numbers, products or functions.
- If something is not in the context, write "Not disclosed" (or leave scale empty).

LTM OPPORTUNITY (fit, fit_rationale, services, engagement_model, talking_points, next_step):
- Recommend ONLY from this offering:
{ltm.CATALOGUE_TEXT}
- services: choose 2-4 names exactly from: {", ".join(ltm.SERVICES)}.
- engagement_model: choose exactly one of: {", ".join(ltm.ENGAGEMENT_MODELS)}.
- fit must be CALIBRATED so leaders can prioritise; typically only the top ~25% of events are High:
  High = a NEW GCC still being set up or scaling fast with a stated large headcount or explicit AI/engineering
  mandate, where build, talent, site or advisory work is clearly still open;
  Medium = expansion of an established GCC, or a new GCC with few details (operate/transform, AI agents);
  Low = small or unclear scale, already fully built, or clearly run with another provider.
  When unsure, choose the lower rating.
- scale: short, e.g. "500 employees" or "250-300 by end-2026; INR 350 cr"; empty if not stated.
- talking_points: 2-3 crisp, specific points that link the company's stated mandate to an LTM capability.
- next_step: one concrete action for the LTM account team (who to approach and with what).
- Be specific and executive-ready. No marketing fluff, no claims about LTM beyond the offering above."""


def _fetch_text(url: str, limit: int = 3500) -> str:
    if not url:
        return ""
    try:
        resp = httpx.get(url, headers={"User-Agent": USER_AGENT}, follow_redirects=True, timeout=15)
        return _page_text(resp.text)[:limit] if resp.status_code == 200 else ""
    except httpx.HTTPError:
        return ""


def gather_context(settings: Settings, item: GCC) -> str:
    parts = [
        f"Event: {item.company} - {'new GCC' if item.event_type == 'new_gcc' else 'GCC expansion'} "
        f"({item.status}) in {item.city}{', ' + item.state if item.state else ''}, India, on {item.event_date}.",
        f"Sector: {item.sector}. Headcount mentioned: {item.headcount or 'not stated'}.",
        f"Summary: {item.summary}",
    ]
    page = _fetch_text(item.source_url)
    if page:
        parts.append(f"--- Source page ({item.source_url}) ---\n{page}")
    if settings.serper_key:
        hits = _serper_organic(settings, f'"{item.company}" company headquarters products business')[:5]
        if hits:
            parts.append("--- Company profile search ---\n" + "\n".join(
                f"- {h.get('title', '')}: {h.get('snippet', '')}" for h in hits))
    return "\n\n".join(parts)


def _clean(insight: LTMInsight) -> LTMInsight:
    """Keep the recommendation inside the real catalogue even if the model drifts."""
    services = [s for s in insight.services if s in ltm.SERVICES] or ["Design & advisory"]
    model = insight.engagement_model if insight.engagement_model in ltm.ENGAGEMENT_MODELS else "Assisted GCC"
    return insight.model_copy(update={
        "services": services[:4], "engagement_model": model,
        "products": [p for p in insight.products if p.strip()][:5],
        "talking_points": [t for t in insight.talking_points if t.strip()][:3],
    })


@trace_tool("ltm_insight", run_type="chain")
def analyse(settings: Settings, item: GCC) -> LTMInsight:
    context = gather_context(settings, item)
    llm = chat(settings, judge=True, temperature=0.2).with_structured_output(LTMInsight)
    result = llm.invoke([SystemMessage(content=INSIGHT_SYSTEM), HumanMessage(content=context)])
    return _clean(result)


def enrich(settings: Settings, items: list[GCC]) -> list[GCC]:
    """Return the items with `insight` filled (items that already have one are left alone)."""
    def one(item: GCC) -> GCC:
        if item.insight is not None:
            return item
        try:
            return item.model_copy(update={"insight": analyse(settings, item)})
        except Exception as exc:  # never lose an item because the brief could not be written
            log.warning("insight failed for %s: %s", item.company, type(exc).__name__)
            return item

    with ThreadPoolExecutor(max_workers=4) as pool:
        return map_in_context(pool, one, items)
