"""Backfill helpers: month windows, catalogue rules, GCC Index row parsing, catalogue storage."""
import json
from datetime import date

from conftest import make_gcc

from gcc_agents import backfill, db, email_agent
from gcc_agents.sites import gccindex


def test_months_and_google_month_filter():
    ms = backfill.months(date(2026, 1, 1), date(2026, 3, 10))
    assert ms == [(date(2026, 1, 1), date(2026, 1, 31)), (date(2026, 2, 1), date(2026, 2, 28)),
                  (date(2026, 3, 1), date(2026, 3, 10))]
    assert backfill.month_filter(*ms[1]) == "cdr:1,cd_min:2/1/2026,cd_max:2/28/2026"


def test_catalog_rules():
    s, e = date(2026, 1, 1), date(2026, 11, 2)
    assert backfill.catalog_reason(make_gcc(event_date=date(2026, 5, 1)), s, e) is None
    assert "outside" in backfill.catalog_reason(make_gcc(event_date=date(2025, 12, 31)), s, e)
    assert "IT-services" in backfill.catalog_reason(make_gcc(company="Wipro", event_date=date(2026, 5, 1)), s, e)
    assert backfill.catalog_reason(make_gcc(city="unknown", event_date=date(2026, 5, 1)), s, e) == "no city stated"


def test_gccindex_rows_without_id_keep_same_day_events_in_different_cities():
    feed = json.dumps([
        {"companyName": "RSM", "city": "Hyderabad", "eventType": "EXPANSION", "announcementDate": "2026-10-29"},
        {"companyName": "RSM", "city": "Bengaluru", "eventType": "EXPANSION", "announcementDate": "2026-10-29"},
        {"companyName": "RSM", "city": "Bengaluru", "eventType": "EXPANSION", "announcementDate": "2026-10-29"},
        {"companyName": "Old", "city": "Pune", "eventType": "NEW_GCC", "announcementDate": "2025-12-30"},
    ])
    rows = gccindex.parse_rows([feed], date(2026, 1, 1), date(2026, 11, 2))
    assert sorted(r.city for r in rows) == ["Bengaluru", "Hyderabad"]


def test_save_catalog_skips_known_events_and_marks_origin():
    with db.connect() as conn:
        first = db.save_catalog(conn, [make_gcc(), make_gcc(company="Beta", press_release_url="https://b.example/1")])
        again = db.save_catalog(conn, [make_gcc(company="ACME Inc", city="Bangalore")])   # same event as Acme Corp
        assert (first, again) == (2, 0)
        assert {r["origin"] for r in conn.execute("SELECT origin FROM gcc")} == {"backfill"}


def test_backfill_stops_at_the_last_emailed_week():
    from datetime import datetime
    with db.connect() as conn:
        assert backfill.default_end(conn, date(2026, 11, 3)) == date(2026, 11, 3)
        db.save_report(conn, window_start=date(2026, 10, 19), window_end=date(2026, 11, 2), items=[],
                       email_status="sent", sent_at=datetime.now(), html_path=None, source_status={})
        assert backfill.default_end(conn, date(2026, 11, 3)) == date(2026, 11, 2)


def test_recipient_parsing():
    assert email_agent.parse_recipients("a@ltm.com; b@ltm.com,  A@ltm.com\nc@x.co") == ["a@ltm.com", "b@ltm.com", "c@x.co"]
    for bad in ("a@", "a b@c.com", "<a@b.com>", "x@y"):
        try:
            email_agent.parse_recipients(bad)
            raise AssertionError(bad)
        except ValueError:
            pass


def test_reclassify_only_changes_other_rows(monkeypatch, settings):
    from gcc_agents import backfill as bf

    with db.connect() as conn:
        db.save_catalog(conn, [make_gcc(company="Ameriprise India", sector="Other", press_release_url="https://a/1"),
                               make_gcc(company="Acme Bank", sector="Technology", press_release_url="https://a/2")])
        ids = {r["company"]: r["id"] for r in conn.execute("SELECT id, company FROM gcc")}

    class FakeLLM:
        def with_structured_output(self, schema):
            self.schema = schema
            return self

        def invoke(self, messages):
            assert "Acme Bank" not in messages[1].content          # only 'Other' rows are sent
            return self.schema(rows=[{"id": ids["Ameriprise India"], "sector": "BFSI"}, {"id": 999, "sector": "Energy"}])

    monkeypatch.setattr("gcc_agents.llm.chat", lambda *a, **k: FakeLLM())
    assert bf.reclassify_sectors(settings) == 1
    with db.connect() as conn:
        assert {r["company"]: r["sector"] for r in conn.execute("SELECT company, sector FROM gcc")} == {
            "Ameriprise India": "BFSI", "Acme Bank": "Technology"}


def test_only_hits_dated_inside_the_month_are_used():
    hits = [{"title": "a", "date": "29 Jan 2026"}, {"title": "undated", "date": ""},
            {"title": "later", "date": "Oct 1, 2026"}, {"title": "b", "date": "Jan 3, 2026"}]
    got = backfill.dated_hits(hits, date(2026, 1, 1), date(2026, 1, 31))
    assert [h["title"] for h in got] == ["a", "b"]


def test_one_new_gcc_per_company_and_city_prefers_gcc_index():
    google_early = make_gcc(company="Kuehne+Nagel", city="Chennai", event_date=date(2026, 1, 29),
                            discovery_sources=["serper"], source_urls=["https://news.example/kn-123456"])
    index_row = make_gcc(company="Kuehne+Nagel", city="Chennai", event_date=date(2026, 10, 1),
                         discovery_sources=["gccindex"], source_urls=[])
    repeat = [make_gcc(company="Starbucks", city="Chennai", event_date=date(2026, m, 1), discovery_sources=["serper"])
              for m in (3, 5, 8)]
    expansion = make_gcc(company="Kuehne+Nagel", city="Chennai", event_type="expansion", event_date=date(2026, 11, 1))
    out = backfill.consolidate_new_gccs([google_early, index_row, *repeat, expansion])
    kn = [i for i in out if i.company == "Kuehne+Nagel" and i.event_type == "new_gcc"]
    assert len(kn) == 1 and kn[0].event_date == date(2026, 10, 1)            # GCC Index date wins
    assert kn[0].source_urls == ["https://news.example/kn-123456"]           # Google link folded in
    assert [i.event_date for i in out if i.company == "Starbucks"] == [date(2026, 3, 1)]   # earliest kept
    assert any(i.event_type == "expansion" for i in out)                     # expansions untouched
