"""Domain models and the normalisation rules that drive de-duplication."""
import re
from datetime import date
from typing import Literal, Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit  # noqa: F401 (urlsplit used below)

from pydantic import BaseModel, Field

SECTORS = (
    "BFSI",
    "Technology",
    "Healthcare/Pharma",
    "Retail/CPG",
    "Manufacturing/Auto",
    "Energy",
    "Other",
)

_COMPANY_NOISE = {
    "pvt", "ltd", "private", "limited", "inc", "incorporated", "llc", "llp", "corp", "corporation",
    "co", "company", "plc", "gmbh", "ag", "sa", "india", "gcc", "gccs", "the", "global", "capability",
    "center", "centre",
}

_CITY_ALIASES = {
    "bangalore": "bengaluru", "bengalore": "bengaluru", "bengaluru": "bengaluru", "blr": "bengaluru",
    "gurgaon": "gurugram", "gurugram": "gurugram",
    "bombay": "mumbai", "navi mumbai": "mumbai",
    "madras": "chennai",
    "calcutta": "kolkata",
    "poona": "pune",
    "cochin": "kochi",
    "trivandrum": "thiruvananthapuram",
    "vizag": "visakhapatnam",
    "mysore": "mysuru",
    "new delhi": "delhi",
    "greater noida": "noida",
    "gautam buddh nagar": "noida", "gautam buddha nagar": "noida", "gb nagar": "noida",
}

_TRACKING_PARAMS = ("utm_", "fbclid", "gclid", "mc_", "ref", "source")


def normalize_company(name: str) -> str:
    text = re.sub(r"[^a-z0-9& ]+", " ", (name or "").casefold()).replace("&", " and ")
    tokens = [t for t in text.split() if t not in _COMPANY_NOISE]
    return " ".join(tokens) or (name or "").casefold().strip()


def canonical_city(city: str) -> str:
    text = (city or "").split(",")[0].strip().casefold()
    text = re.sub(r"\s+", " ", text)
    return _CITY_ALIASES.get(text, text)


def canonical_url(url: str) -> str:
    if not url:
        return ""
    parts = urlsplit(url.strip())
    if not parts.netloc:
        return ""
    query = [(k, v) for k, v in parse_qsl(parts.query) if not k.lower().startswith(_TRACKING_PARAMS)]
    host = parts.netloc.lower().removeprefix("www.")
    path = parts.path.rstrip("/") or ""
    return urlunsplit((parts.scheme.lower() or "https", host, path, urlencode(query), ""))


# ---------------------------------------------------------------- "is this the same GCC event?"
_GENERIC_FIRST_WORDS = {"bank", "first", "national", "united", "american", "general", "global", "new", "royal", "state"}
SAME_EVENT_DAYS = 7  # two reports of one company in one city within this many days describe one event


def same_company(a: str, b: str) -> bool:
    """Company keys that name the same firm: equal, or one is a leading-word prefix of the other
    ("rsm" vs "rsm us integrated services", "metlife" vs "metlife operations support"). A single generic word
    never matches on its own."""
    if a == b:
        return True
    ta, tb = a.split(), b.split()
    short, long_ = (ta, tb) if len(ta) <= len(tb) else (tb, ta)
    if not short or len(short[0]) < 3 or short[0] in _GENERIC_FIRST_WORDS:
        return False
    return long_[: len(short)] == short


def same_group(a: str, b: str) -> bool:
    """Same leading brand word, even a short one ("lg soft" / "lg electronics"). Only used with the same city and a
    few days apart, where two different firms sharing a first word is very unlikely."""
    ta, tb = a.split(), b.split()
    return bool(ta and tb) and ta[0] == tb[0] and ta[0] not in _GENERIC_FIRST_WORDS


def same_event(company_a: str, city_a: str, type_a: str, date_a, company_b: str, city_b: str, type_b: str,
               date_b) -> bool:
    """One rule for "these two records describe the same GCC event", used when merging candidates, when checking
    the database before saving, and when cleaning up the database.

    Same city, and:
      - the same company within SAME_EVENT_DAYS, whatever each source called it (new GCC vs expansion), or
      - the same brand family (LG Soft / LG Electronics) within SAME_EVENT_DAYS, or
      - the same company with a "new GCC" on both sides, at any date (a GCC is new once), or
      - the same company with an expansion on both sides in the same month.
    Takes normalised keys (company_key, city_key)."""
    if city_a != city_b or not city_a:
        return False
    sc = same_company(company_a, company_b)
    if not sc and not same_group(company_a, company_b):
        return False
    gap = abs((date_a - date_b).days) if date_a and date_b else None
    if gap is not None and gap <= SAME_EVENT_DAYS:
        return True
    if not sc:
        return False
    if type_a == type_b == "new_gcc":
        return True
    return (type_a == type_b == "expansion" and date_a is not None and date_b is not None
            and (date_a.year, date_a.month) == (date_b.year, date_b.month))


class ExtractedItem(BaseModel):
    """Schema handed to the LLM (plain strings only — dates are parsed afterwards)."""

    company: str = Field(description="Company that owns/operates the GCC (parent, not the India entity name)")
    city: str = Field(description="Indian city of the centre; empty string if not stated")
    state: str = Field(default="", description="Indian state, if known")
    sector: str = Field(default="Other", description="Business sector of the company")
    event_type: Literal["new_gcc", "expansion"] = Field(
        description="new_gcc = company sets up/launches/opens a GCC in India; "
        "expansion = an existing India GCC adds a new centre/facility/city or major seats/headcount"
    )
    status: Literal["opened", "announced"] = Field(
        description="opened = officially opened/inaugurated; announced = officially announced by the company"
    )
    event_date: str = Field(description="Date of the opening/announcement as YYYY-MM-DD; empty string if unknown")
    reported_date: str = Field(
        default="", description="Latest 'Published' date (YYYY-MM-DD) of the source items reporting this event"
    )
    headcount: str = Field(default="", description="Employees/seats mentioned, free text; empty if not stated")
    summary: str = Field(description="One factual sentence, using only facts in the source text")
    press_release_url: str = Field(
        default="", description="URL of an OFFICIAL press release / company newsroom / government source, else empty"
    )
    source_urls: list[str] = Field(default_factory=list, description="URLs of articles/posts that reported it")


class ExtractionResult(BaseModel):
    items: list[ExtractedItem] = Field(default_factory=list)


class LTMInsight(BaseModel):
    """Account intelligence for one GCC, written from LTM's point of view (filled by the insights agent)."""

    headquarters: str = Field(default="", description="Parent company HQ city/country, or empty if not known")
    business_area: str = Field(description="What the company does: industry and core business, one sentence")
    products: list[str] = Field(default_factory=list, description="2-5 key products, brands or service lines")
    gcc_mandate: str = Field(description="What the India centre will do (functions, tech areas), from the sources")
    scale: str = Field(default="", description="Headcount / seats / investment stated in the sources, else empty")
    fit: Literal["High", "Medium", "Low"] = Field(description="Opportunity for LTM GCC Origin")
    fit_rationale: str = Field(description="Why this fit, 1-2 sentences, grounded in the facts above")
    services: list[str] = Field(default_factory=list, description="GCC Origin services that apply")
    engagement_model: str = Field(description="Best-fit GCC Origin engagement model")
    talking_points: list[str] = Field(default_factory=list, description="2-3 short points for an account conversation")
    next_step: str = Field(description="One concrete next step for the LTM account team")


class GCC(BaseModel):
    company: str
    city: str
    state: str = ""
    sector: str = "Other"
    event_type: Literal["new_gcc", "expansion"]
    status: Literal["opened", "announced"] = "announced"
    event_date: Optional[date] = None
    reported_date: Optional[date] = None  # when the news appeared (can be later than the event itself)
    headcount: str = ""
    summary: str = ""
    press_release_url: str = ""  # an official-release URL *claimed* by extraction; verified in evaluate
    source_urls: list[str] = Field(default_factory=list)
    discovery_sources: list[str] = Field(default_factory=list)
    # The link shown in the report, chosen in evaluate: press release if one exists, else company post, else news.
    source_url: str = ""
    source_kind: Literal["press_release", "company_post", "news", "post"] = "news"
    insight: Optional[LTMInsight] = None  # LTM opportunity analysis, added after evaluation

    @property
    def source_label(self) -> str:
        if self.source_kind == "press_release":
            return "Press release"
        if self.source_kind == "company_post":
            return "Company post"
        if self.source_kind == "post":
            return "Post"
        return "News"

    @property
    def source_host(self) -> str:
        """Short site name shown under the link for news/post sources (empty for press releases and company posts)."""
        if self.source_kind not in ("news", "post"):
            return ""
        parts = urlsplit(self.source_url).netloc.lower().removeprefix("www.").split(".")
        if len(parts) >= 3 and parts[-2] in {"co", "com", "org", "net", "gov", "ac"} and len(parts[-1]) == 2:
            return ".".join(parts[-3:])  # e.g. bbc.co.uk, thehindu.co.in
        return ".".join(parts[-2:])

    @property
    def company_key(self) -> str:
        return normalize_company(self.company)

    @property
    def city_key(self) -> str:
        return canonical_city(self.city)

    @property
    def event_key(self) -> str:
        base = f"{self.company_key}|{self.city_key}|{self.event_type}"
        if self.event_type == "expansion":
            base += f"|{self.event_date.strftime('%Y-%m') if self.event_date else 'unknown'}"
        return base

    @classmethod
    def from_extracted(cls, item: ExtractedItem, source: str) -> "GCC":
        def parse(value: str) -> Optional[date]:
            try:
                return date.fromisoformat(value.strip()[:10]) if value.strip() else None
            except ValueError:
                return None

        # No explicit event date: fall back to the news date (the extraction prompt asks for the same).
        parsed = parse(item.event_date) or parse(item.reported_date)
        return cls(
            company=item.company.strip(),
            city=item.city.strip(),
            state=item.state.strip(),
            sector=item.sector.strip() or "Other",
            event_type=item.event_type,
            status=item.status,
            event_date=parsed,
            reported_date=parse(item.reported_date),
            headcount=item.headcount.strip(),
            summary=item.summary.strip(),
            press_release_url=canonical_url(item.press_release_url) if item.press_release_url else "",
            source_urls=[u for u in dict.fromkeys(item.source_urls) if u],
            discovery_sources=[source],
        )
