"""OpenAI chat-model factory and the shared structured-extraction helper."""
from datetime import date
from typing import Iterable, Optional

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI

from .config import Settings
from .models import ExtractionResult, GCC
from .tracing import trace_tool

EXTRACT_SYSTEM = """You extract India GCC (Global Capability Centre / captive centre) events from raw text.
Only output events that are (a) a company SETTING UP / LAUNCHING / OPENING a GCC in India ("new_gcc"), or
(b) an EXISTING India GCC adding a new centre/facility/city or a major seat/headcount expansion ("expansion").
Rules:
- Use ONLY facts present in the provided text. Never invent dates, cities, companies or URLs.
- Skip: rumours, "in talks"/"considering", hiring-only news, leadership appointments, Indian IT-services
  firms' ordinary office openings, event/conference listings, opinion pieces.
- status "opened" only if it is stated to be opened/inaugurated/launched/operational; otherwise "announced".
- press_release_url: fill ONLY if the text contains an official press release / company newsroom / government
  URL for the event. Never put a news-article URL there (those belong in source_urls).
- event_date: the date of the opening/announcement (YYYY-MM-DD). Each source item may carry a
  "Published:" date; when the text gives no more specific date for the event, USE that Published date.
  Only leave it empty if neither the text nor a Published date gives one.
- sector: one of BFSI, Technology, Healthcare/Pharma, Retail/CPG, Manufacturing/Auto, Energy, Other.
- Return an empty list if nothing qualifies."""


def chat(settings: Settings, judge: bool = False, temperature: float = 0.0) -> ChatOpenAI:
    return ChatOpenAI(model=settings.judge_model if judge else settings.openai_model, temperature=temperature)


@trace_tool("extract_items", run_type="chain")
def extract_items(
    settings: Settings,
    text: str,
    source: str,
    *,
    today: date,
    already_reported: Iterable[str] = (),
    max_chars: int = 24000,
    default_date: Optional[date] = None,
) -> list[GCC]:
    """Run structured extraction on a blob of text and tag the results with their discovery source."""
    if not text.strip():
        return []
    llm = chat(settings).with_structured_output(ExtractionResult)
    excluded = "; ".join(list(already_reported)[:150]) or "none"
    prompt = (
        f"Today is {today.isoformat()}.\n"
        f"Already reported earlier (do NOT return these again): {excluded}\n\n"
        f"--- SOURCE TEXT ({source}) ---\n{text[:max_chars]}"
    )
    result = llm.invoke([SystemMessage(content=EXTRACT_SYSTEM), HumanMessage(content=prompt)])
    items = [GCC.from_extracted(i, source) for i in result.items if i.company.strip()]
    return fill_missing_dates(items, default_date)


def fill_missing_dates(items: list[GCC], default_date: Optional[date]) -> list[GCC]:
    """Items the model left undated take the source document's publish date (when we know it)."""
    if default_date is None:
        return items
    return [i if i.event_date else i.model_copy(update={"event_date": default_date}) for i in items]
