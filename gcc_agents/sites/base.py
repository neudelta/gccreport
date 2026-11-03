"""Shared helpers for the site adapters."""
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Optional

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0 Safari/537.36"
)

# Titles worth a full-article fetch: launches / openings / expansions, not trend or people pieces.
EVENT_TITLE = re.compile(
    r"\b(open(s|ed|ing)?|launch(es|ed)?|inaugurat\w*|set(s|ting)?( up)?|expan\w*|establish\w*|unveil\w*|"
    r"debut\w*|enter(s|ed|ing)?|announce\w*|foray|second|third|gccs?|hub|campus|centre|center|office|facility|"
    r"tech park|investment|invest(s|ing)?|cabinet|mou|letter of comfort)\b",
    re.I,
)


class SiteBlocked(Exception):
    """The site refused automated access (bot protection)."""


class SiteSkipped(Exception):
    """The adapter is not configured (e.g. missing credentials)."""


@dataclass
class Doc:
    url: str
    text: str
    published: Optional[date] = None
    items: Optional[list[Any]] = None  # ready-made GCC items when the site is already structured (no LLM needed)


def in_window(d: Optional[date], today: date, lookback_days: int) -> bool:
    """True if d is within [today - lookback - 1 day slack, today + 1 day]. Unknown dates are kept."""
    if d is None:
        return True
    return today - timedelta(days=lookback_days + 1) <= d <= today + timedelta(days=1)


def parse_any_date(value: str) -> Optional[date]:
    if not value:
        return None
    value = value.strip()
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
    except ValueError:
        pass
    for fmt in ("%B %d, %Y", "%b %d, %Y", "%d %B %Y", "%d %b %Y"):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    return None


def chunk_docs(docs: list[Doc], max_chars: int = 18000, max_docs: int = 10) -> list[str]:
    """Group docs into text blobs for extraction calls."""
    chunks, cur, size = [], [], 0
    for d in docs:
        block = f"URL: {d.url}\nPublished: {d.published or 'unknown'}\n{d.text.strip()}\n"
        if cur and (size + len(block) > max_chars or len(cur) >= max_docs):
            chunks.append("\n=====\n".join(cur))
            cur, size = [], 0
        cur.append(block)
        size += len(block)
    if cur:
        chunks.append("\n=====\n".join(cur))
    return chunks
