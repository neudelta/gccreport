"""Environment-driven settings. Secrets come from .env and are never logged."""
import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

DATA_DIR = ROOT / "data"
REPORTS_DIR = DATA_DIR / "reports"
DB_PATH = DATA_DIR / "gcc.db"
GCCINDEX_STATE = DATA_DIR / "gccindex_state.json"

ALL_SOURCES = ("serper", "groq_x", "et_gcc", "gcc_pulse", "moneycontrol", "gccindex")


def _bool(name: str, default: bool) -> bool:
    return os.getenv(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


@dataclass
class Settings:
    openai_model: str = field(default_factory=lambda: os.getenv("OPENAI_MODEL", "gpt-4o-mini"))
    judge_model: str = field(default_factory=lambda: os.getenv("OPENAI_JUDGE_MODEL", "gpt-4o"))
    serper_key: str = field(default_factory=lambda: os.getenv("SERPER_API_KEY", ""), repr=False)
    groq_key: str = field(default_factory=lambda: os.getenv("GROQ_API_KEY", ""), repr=False)
    groq_model: str = field(default_factory=lambda: os.getenv("GROQ_MODEL", "groq/compound-mini"))
    gccindex_user: str = field(default_factory=lambda: os.getenv("GCCINDEX_USERNAME", ""), repr=False)
    gccindex_pass: str = field(default_factory=lambda: os.getenv("GCCINDEX_PASSWORD", ""), repr=False)
    gmail_user: str = field(default_factory=lambda: os.getenv("GMAIL_USER", ""), repr=False)
    gmail_password: str = field(default_factory=lambda: os.getenv("GMAIL_APP_PASSWORD", ""), repr=False)
    to_email: str = field(default_factory=lambda: os.getenv("TO_EMAIL", ""), repr=False)
    lookback_days: int = field(default_factory=lambda: int(os.getenv("LOOKBACK_DAYS", "14")))
    send_empty_report: bool = field(default_factory=lambda: _bool("SEND_EMPTY_REPORT", True))
    include_announced: bool = field(default_factory=lambda: _bool("INCLUDE_ANNOUNCED", True))
    disabled_sources: set = field(
        default_factory=lambda: {s.strip() for s in os.getenv("DISABLED_SOURCES", "").split(",") if s.strip()}
    )
    only_sources: set = field(default_factory=set)  # set by --only

    def source_enabled(self, name: str) -> bool:
        if self.only_sources:
            return name in self.only_sources
        return name not in self.disabled_sources


def get_settings() -> Settings:
    return Settings()
