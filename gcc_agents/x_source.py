"""X.com lead source: Groq Compound web search restricted to x.com, then OpenAI extraction.

X posts are leads only - the evaluate step still requires an official press release.
"""
from datetime import date, timedelta

from .config import Settings
from .llm import extract_items
from .models import GCC
from .tracing import trace_tool

PROMPT = """Search X (Twitter) for posts from {start} to {end} about a company opening, launching or
expanding a Global Capability Centre (GCC) in India (new GCC, new centre, new facility, expansion).
For each post you actually find, list: company, city, what happened, the post date, the post URL, and any
press-release / newsroom link mentioned in the post. Only report posts you really found; if none, say so."""


@trace_tool("groq_compound_x_search", run_type="llm", metadata={"ls_provider": "groq"})
def _groq_search(settings: Settings, prompt: str) -> str:
    """One Groq Compound call restricted to x.com; returns the answer plus the raw search results."""
    from groq import Groq

    client = Groq(api_key=settings.groq_key)
    completion = client.chat.completions.create(
        model=settings.groq_model,
        messages=[{"role": "user", "content": prompt}],
        search_settings={"include_domains": ["x.com", "twitter.com"]},
    )
    msg = completion.choices[0].message
    parts = [msg.content or ""]
    # Fold the raw search results in as well, so extraction sees post text and URLs.
    for tool in getattr(msg, "executed_tools", None) or []:
        results = getattr(getattr(tool, "search_results", None), "results", None) or []
        for r in results:
            parts.append(f"{getattr(r, 'title', '')}\n{getattr(r, 'url', '')}\n{getattr(r, 'content', '')}")
    return "\n\n".join(p for p in parts if p.strip())


def run(settings: Settings, today: date, already_reported: list[str]) -> tuple[list[GCC], str]:
    if not settings.groq_key:
        return [], "skipped: GROQ_API_KEY not set"
    try:
        start = today - timedelta(days=settings.lookback_days)
        text = _groq_search(settings, PROMPT.format(start=start.isoformat(), end=today.isoformat()))
        items = extract_items(settings, text, "groq_x", today=today, already_reported=already_reported)
        return items, f"ok: {len(items)} candidates"
    except Exception as exc:
        return [], f"failed: {type(exc).__name__}: {exc}"[:300]
