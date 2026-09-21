"""Date parsing for messy web dates ("2 days ago", "Sep 18, 2026", ISO timestamps)."""
import re
from datetime import date, datetime, timedelta
from typing import Optional

_RELATIVE = re.compile(r"(\d+)\s*(minute|min|hour|hr|day|week|month)s?\s+ago", re.I)
_UNIT_DAYS = {"minute": 0, "min": 0, "hour": 0, "hr": 0, "day": 1, "week": 7, "month": 30}
_FORMATS = ("%B %d, %Y", "%b %d, %Y", "%d %B %Y", "%d %b %Y", "%b %d %Y", "%d/%m/%Y")


def parse_web_date(value: str, today: date) -> Optional[date]:
    """Best-effort absolute date for a scraped/search-result date string; None if it can't be understood."""
    if not value:
        return None
    text = value.strip().replace("Sept", "Sep")
    m = _RELATIVE.search(text)
    if m:
        return today - timedelta(days=int(m.group(1)) * _UNIT_DAYS[m.group(2).lower()])
    if text.lower() in ("today", "just now", "yesterday"):
        return today - timedelta(days=1 if text.lower() == "yesterday" else 0)
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        pass
    for fmt in _FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None
