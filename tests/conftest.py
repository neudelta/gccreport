import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gcc_agents import config  # noqa: E402
from gcc_agents.models import GCC, LTMInsight  # noqa: E402

TODAY = date(2026, 9, 19)


@pytest.fixture(autouse=True)
def tmp_data(tmp_path, monkeypatch):
    """Point every test at a throwaway data dir/DB."""
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "gcc.db")
    monkeypatch.setattr(config, "REPORTS_DIR", tmp_path / "reports")
    return tmp_path


FAKE_INSIGHT = LTMInsight(
    headquarters="Perth, Australia", business_area="Oil and gas producer", products=["LNG", "Oil"],
    gcc_mandate="AI, cybersecurity and data engineering", scale="150", fit="High",
    fit_rationale="New centre still scaling its AI and engineering teams.",
    services=["Talent & workforce", "AI agents & studios"], engagement_model="Build-Operate-Transform-Transfer",
    talking_points=["Scale AI talent fast", "Agentic operations from day one"],
    next_step="Brief the India centre head on BlueVerse agents for engineering.",
)


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    """No test may call OpenAI or launch Chromium for the PDF."""
    from gcc_agents import insights_agent, report_agent
    monkeypatch.setattr(insights_agent, "analyse", lambda settings, item: FAKE_INSIGHT)
    monkeypatch.setattr(report_agent, "render_pdf", lambda *a, **k: b"%PDF-1.7 fake")
    monkeypatch.setattr(report_agent, "executive_summary", lambda s, i: "Summary.")


@pytest.fixture
def settings():
    s = config.Settings()
    # Never use real credentials from .env in tests: overwrite every secret with a dummy value.
    s.serper_key, s.groq_key = "test-serper", "test-groq"
    s.gccindex_user, s.gccindex_pass = "", ""
    s.gmail_user, s.gmail_password, s.to_email = "me@example.com", "app pass word", "me@example.com"
    s.lookback_days, s.send_empty_report, s.include_announced = 7, True, True
    s.disabled_sources, s.only_sources = set(), set()
    return s


def make_gcc(company="Acme Corp", city="Bengaluru", event_type="new_gcc", days_ago=2, **kw) -> GCC:
    from datetime import timedelta

    base = dict(
        company=company, city=city, state="Karnataka", sector="Technology", event_type=event_type,
        status="opened", event_date=TODAY - timedelta(days=days_ago), summary="Opened a centre.",
        press_release_url="https://www.acme.com/news/gcc-bengaluru", source_urls=["https://news.example/a"],
        source_url="https://www.acme.com/news/gcc-bengaluru", source_kind="press_release",
        discovery_sources=["serper"],
    )
    base.update(kw)
    if "press_release_url" in kw and "source_url" not in kw:
        base["source_url"] = kw["press_release_url"]
    return GCC(**base)
