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


def test_generic_taglines_are_not_companies(settings):
    from conftest import TODAY
    from gcc_agents.evaluate_agent import is_generic_name, precheck
    assert is_generic_name("transport and logistics")                       # "Global Transport and Logistics"
    assert not is_generic_name("dsv") and not is_generic_name("clean harbors") and not is_generic_name("us bancorp")
    assert "not a company" in precheck(make_gcc(company="Global Transport and Logistics"), TODAY, settings)



def test_same_event_rule_covers_the_duplicates_found_on_3_nov():
    from datetime import date
    from gcc_agents.models import normalize_company, same_event

    def same(a, at, ad, b, bt, bd, city="noida"):
        return same_event(normalize_company(a), city, at, date.fromisoformat(ad),
                          normalize_company(b), city, bt, date.fromisoformat(bd))

    assert same("MetLife", "new_gcc", "2026-10-27", "MetLife Global Operations Support Center Private Limited",
                "new_gcc", "2026-10-24")
    assert same("LG Soft", "new_gcc", "2026-10-27", "LG Electronics", "new_gcc", "2026-10-24")
    assert same("Truist", "expansion", "2026-10-21", "Truist Financial Corporation", "expansion", "2026-10-21")
    assert same("SonicWall", "new_gcc", "2026-10-26", "SonicWall", "expansion", "2026-10-23")   # label differs
    assert same("RSM US", "new_gcc", "2026-10-29", "RSM", "expansion", "2026-10-29")
    # still different events
    assert not same("LG Soft", "new_gcc", "2026-10-27", "LG Electronics", "new_gcc", "2026-08-01")  # brand, far apart
    assert not same("Acme", "expansion", "2026-03-01", "Acme", "expansion", "2026-09-01")            # later expansion
    assert not same("Bank of America", "new_gcc", "2026-10-01", "Bank of India", "new_gcc", "2026-10-02")
    assert not same_event("rsm", "hyderabad", "new_gcc", date(2026, 10, 29), "rsm", "bengaluru", "new_gcc",
                          date(2026, 10, 29))                                                       # other city


def test_database_check_and_cleanup_use_the_same_rule():
    from datetime import date, datetime
    from gcc_agents import db
    with db.connect() as conn:
        db.save_report(conn, window_start=date(2026, 10, 19), window_end=date(2026, 11, 2), email_status="sent",
                       sent_at=datetime.now(), html_path=None, source_status={},
                       items=[make_gcc(company="MetLife", city="Noida", event_date=date(2026, 10, 27),
                                       press_release_url="https://m.example/1")])
        later = make_gcc(company="MetLife Global Operations Support Center Private Limited",
                         city="Gautam Buddh Nagar", event_date=date(2026, 10, 24), press_release_url="")
        assert db.is_duplicate(conn, later) and db.is_reported(conn, later)       # blocked at save time now
        assert db.save_catalog(conn, [later]) == 0
        # an older database that already holds both: clean-up keeps the emailed row and folds in the other
        conn.execute("INSERT INTO gcc (company, company_key, city, city_key, event_type, status, event_date,"
                     " event_key, first_reported_at, origin, source_urls, discovery_sources)"
                     " VALUES ('MetLife GOSC Pvt Ltd', 'metlife gosc', 'Noida', 'noida', 'new_gcc', 'announced',"
                     " '2026-10-24', 'metlife gosc|noida|new_gcc', '2026-11-03', 'backfill', '[\"https://x/1\"]',"
                     " '[\"gccindex\"]')")
        assert len(db.merge_duplicates(conn, dry_run=True)) == 1
        assert conn.execute("SELECT COUNT(*) FROM gcc").fetchone()[0] == 2          # dry run changes nothing
        merged = db.merge_duplicates(conn)
        assert len(merged) == 1 and merged[0][0].startswith("MetLife (Noida")
        row = conn.execute("SELECT company, report_id, discovery_sources, source_urls FROM gcc").fetchall()
        assert len(row) == 1 and row[0]["company"] == "MetLife" and row[0]["report_id"] is not None
        assert "gccindex" in row[0]["discovery_sources"] and "https://x/1" in row[0]["source_urls"]
