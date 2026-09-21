import json
from datetime import date

from gcc_agents.sites import et_gcc, gcc_pulse
from gcc_agents.sites.base import Doc, chunk_docs, in_window, parse_any_date

TODAY = date(2026, 9, 19)


def test_et_listing_filters_to_event_titles_and_cleans_urls():
    base = "https://gcc.economictimes.indiatimes.com"
    anchors = [
        ("Woodside Energy opens Bengaluru global capability centre",
         f"{base}/news/workplace-transformation/woodside-energy-opens-bengaluru-global-capability-centre/134288381?utm_source=newslisting"),
        ("Faces of Change: Suja Chandy", f"{base}/news/people-culture/faces-of-change-suja-chandy/134329834?utm_source=x"),
        ("Woodside Energy opens Bengaluru global capability centre",
         f"{base}/news/workplace-transformation/woodside-energy-opens-bengaluru-global-capability-centre/134288381"),
        ("Events", f"{base}/events"),
        ("Riveron expands India operations with new Bengaluru office",
         f"{base}/news/workplace-transformation/riveron-expands-india-operations-with-new-bengaluru-office/134235781"),
    ]
    out = et_gcc.parse_listing(anchors)
    assert [t for t, _ in out] == [
        "Woodside Energy opens Bengaluru global capability centre",
        "Riveron expands India operations with new Bengaluru office",
    ]
    assert all("?" not in u for _, u in out)


def test_et_article_json_ld():
    blobs = [
        json.dumps({"@type": "NewsMediaOrganization"}),
        json.dumps({"@type": "NewsArticle", "datePublished": "2026-09-16T18:35:54+05:30", "articleBody": "Body text"}),
    ]
    assert et_gcc.parse_article_ld(blobs) == ("2026-09-16T18:35:54+05:30", "Body text")
    assert et_gcc.parse_article_ld(["not json"]) == ("", "")


def test_gcc_pulse_days_filtered_by_window():
    days = [
        {"date": "September 19, 2026", "items": [{"title": "Kraft Heinz Officially Launches Bengaluru Satellite Hub", "text": "..."}]},
        {"date": "August 1, 2026", "items": [{"title": "Old news", "text": "..."}]},
        {"date": "September 17, 2026", "items": [{"title": "", "text": "no title"}]},
    ]
    docs = gcc_pulse.parse_days(days, TODAY, 7)
    assert len(docs) == 1 and "Kraft Heinz" in docs[0].text and docs[0].published == date(2026, 9, 19)


def test_date_helpers():
    assert parse_any_date("September 19, 2026") == date(2026, 9, 19)
    assert parse_any_date("2026-09-16T18:35:54+05:30") == date(2026, 9, 16)
    assert parse_any_date("garbage") is None
    assert in_window(None, TODAY, 7) and not in_window(date(2026, 8, 1), TODAY, 7)


def test_chunk_docs_respects_limits():
    docs = [Doc(url=f"u{i}", text="x" * 100) for i in range(25)]
    chunks = chunk_docs(docs, max_chars=100000, max_docs=10)
    assert len(chunks) == 3


# ---- GCC Index feed ----

GCCINDEX_FEED = json.dumps([
    {"id": "1", "companyName": "Digital Divide Data", "city": "Dehradun", "eventType": "NEW_GCC", "jobsCount": 850,
     "sectorNormalized": "Technology & Software", "announcementDate": "2026-09-18",
     "eventContext": "Digital Divide Data opened an AI and robotics data services centre in Dehradun."},
    {"id": "2", "companyName": "Hexadex", "city": "Pune", "eventType": "EXPANSION", "jobsCount": None,
     "sectorNormalized": "Manufacturing & Industrial", "announcementDate": "2026-09-14", "eventContext": "Launched a Pune GCC."},
    {"id": "3", "companyName": "Old Co", "city": "Chennai", "eventType": "NEW_GCC", "announcementDate": "2026-08-01"},
    {"id": "1", "companyName": "Digital Divide Data", "city": "Dehradun", "eventType": "NEW_GCC", "announcementDate": "2026-09-18"},
    {"companyName": "", "announcementDate": "2026-09-18"},
])


def parse_events_far_past_is_dropped() -> bool:
    from gcc_agents.sites import gccindex
    ancient = json.dumps([{"id": "9", "companyName": "Ancient", "city": "Pune", "eventType": "NEW_GCC",
                           "announcementDate": "2025-01-05"}])
    return gccindex.parse_events([ancient], TODAY, 7) == []


def test_gccindex_feed_parsing_uses_exact_dates_and_window():
    from gcc_agents.sites import gccindex

    items = gccindex.parse_events([GCCINDEX_FEED, "not json", json.dumps({"not": "a list"})], TODAY, 7)
    assert [(i.company, i.event_date) for i in items] == [
        ("Digital Divide Data", date(2026, 9, 18)), ("Hexadex", date(2026, 9, 14)), ("Old Co", date(2026, 8, 1)),
    ]  # de-duplicated by id and blank rows dropped; older rows are kept so evaluate can spot stale re-reports
    ddd, hex_, _old = items
    assert parse_events_far_past_is_dropped()
    assert ddd.event_type == "new_gcc" and ddd.sector == "Technology" and ddd.headcount == "850"
    assert hex_.event_type == "expansion" and hex_.sector == "Manufacturing/Auto" and hex_.headcount == ""
    assert ddd.discovery_sources == ["gccindex"] and ddd.source_urls == []


def test_sites_source_uses_structured_items_without_llm(monkeypatch, settings):
    from gcc_agents import sites_source
    from gcc_agents.sites import gccindex
    from gcc_agents.sites.base import Doc

    items = gccindex.parse_events([GCCINDEX_FEED], TODAY, 7)
    monkeypatch.setattr(sites_source.gccindex, "fetch", lambda *a: [Doc(url="u", text="", items=items)])
    monkeypatch.setattr(sites_source, "extract_items", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no LLM")))
    settings.only_sources = {"gccindex"}
    found, status = sites_source.run(settings, TODAY, [])
    assert len(found) == 3 and status["gccindex"].startswith("ok")
