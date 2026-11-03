"""Regression tests for the coverage gaps found on 2 Nov 2026 (Havells, LG Soft, Ameriprise, MetLife, TIAA,
O'Reilly were missed). Each test pins the specific fix so these sources are not lost again."""
from conftest import make_gcc

from gcc_agents import llm, serper_source
from gcc_agents.evaluate_agent import merge_candidates
from gcc_agents.models import canonical_city
from gcc_agents.sites import et_gcc
from gcc_agents.sites.base import EVENT_TITLE


def test_google_window_follows_the_report_window(settings):
    settings.lookback_days = 14
    assert serper_source.time_filter(settings) == "qdr:d14"   # TIAA (23 Oct) was outside "qdr:w"


def test_et_gcc_reads_the_openings_section():
    assert any("workplace-transformation" in u for u in et_gcc.LISTINGS)   # O'Reilly was only listed there


def test_title_filter_catches_openings_not_called_gcc():
    for t in ("O'Reilly Auto Parts to enter India, targets tech talent", "Mythic AI sets centre in Bengaluru",
              "TIAA Celebrates 10 Years in India, Opens New Pune Office", "UP cabinet clears 4 GCC projects"):
        assert EVENT_TITLE.search(t), t
    assert not EVENT_TITLE.search("Faces of Change: Kailash Maisekar")


def test_search_covers_state_approvals_and_city_gcc_sites():
    qs = " | ".join(serper_source.queries_for(0))
    for phrase in ("cabinet approves", "letter of comfort", "MoU", "site:punegcc.com", "opens new office"):
        assert phrase in qs, phrase


def test_extraction_splits_multi_company_announcements():
    rule = llm.EXTRACT_SYSTEM
    assert "One item PER COMPANY" in rule and "never the government" in rule


def test_noida_district_and_group_merge():
    assert canonical_city("Gautam Buddh Nagar") == canonical_city("Noida") == "noida"
    a = make_gcc(company="LG Soft India", city="Gautam Buddh Nagar", days_ago=6)
    b = make_gcc(company="LG Electronics", city="Noida", days_ago=6)
    c = make_gcc(company="LG Electronics", city="Pune", days_ago=6)
    assert len(merge_candidates([a, b, c])) == 2


def test_it_services_competitors_and_placeholder_cities_are_rejected(settings):
    from conftest import TODAY
    from gcc_agents.evaluate_agent import precheck
    assert "IT-services" in precheck(make_gcc(company="HCLTech"), TODAY, settings)
    assert "IT-services" in precheck(make_gcc(company="Infosys Ltd"), TODAY, settings)
    assert precheck(make_gcc(company="Collins Aerospace", city="unknown"), TODAY, settings) == "no city stated"
    assert precheck(make_gcc(company="Collins Aerospace", city="Not disclosed"), TODAY, settings) == "no city stated"
    assert precheck(make_gcc(company="Havells India", city="Noida"), TODAY, settings) is None


def test_late_coverage_inside_the_window_is_kept(settings):
    from datetime import date
    from gcc_agents.evaluate_agent import precheck
    today = date(2026, 11, 2)
    settings.lookback_days = 14
    tiaa = make_gcc(company="TIAA", city="Pune", event_date=date(2026, 10, 11), reported_date=date(2026, 10, 23))
    assert precheck(tiaa, today, settings) is None                         # reported in window, event 11 Oct
    old = make_gcc(company="Old Co", event_date=date(2026, 9, 1), reported_date=date(2026, 10, 23))
    assert "outside" in precheck(old, today, settings)                     # too old even if re-reported
    unreported = make_gcc(company="X", event_date=date(2026, 10, 11))
    assert "outside" in precheck(unreported, today, settings)              # no news date in window


def test_missing_event_date_falls_back_to_the_news_date():
    from datetime import date
    from gcc_agents.models import GCC, ExtractedItem
    raw = ExtractedItem(company="Havells India", city="Gautam Buddh Nagar", event_type="new_gcc", status="announced",
                        event_date="", reported_date="2026-10-27", summary="UP cabinet cleared Havells GCC.")
    item = GCC.from_extracted(raw, "serper")
    assert item.event_date == date(2026, 10, 27) and item.reported_date == date(2026, 10, 27)
