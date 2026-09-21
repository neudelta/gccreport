from datetime import date

from conftest import TODAY, make_gcc

from gcc_agents.evaluate_agent import is_official_domain, merge_candidates, precheck
from gcc_agents.models import canonical_city, canonical_url, normalize_company


def test_company_normalisation():
    assert normalize_company("Acme India Pvt Ltd") == normalize_company("ACME, Inc.") == "acme"
    assert normalize_company("Woodside Energy") == "woodside energy"


def test_city_aliases():
    assert canonical_city("Bangalore") == canonical_city("Bengaluru, Karnataka") == "bengaluru"
    assert canonical_city("Gurgaon") == "gurugram"


def test_canonical_url_strips_tracking_and_fragment():
    assert canonical_url("https://www.Acme.com/news/x/?utm_source=a&id=3#top") == "https://acme.com/news/x?id=3"
    assert canonical_url("not a url") == ""


def test_event_key_new_vs_expansion():
    new = make_gcc(company="Acme India Pvt Ltd", city="Bangalore")
    same = make_gcc(company="ACME Inc", city="Bengaluru")
    assert new.event_key == same.event_key == "acme|bengaluru|new_gcc"
    exp = make_gcc(event_type="expansion", event_date=date(2026, 9, 10))
    assert exp.event_key.endswith("|expansion|2026-09")


def test_merge_cross_source_duplicates():
    a = make_gcc(discovery_sources=["serper"], press_release_url="", status="announced")
    b = make_gcc(discovery_sources=["et_gcc"], status="opened")
    c = make_gcc(company="Other Co", discovery_sources=["gcc_pulse"])
    merged = merge_candidates([a, b, c])
    assert len(merged) == 2
    acme = next(m for m in merged if m.company_key == "acme")
    assert set(acme.discovery_sources) == {"serper", "et_gcc"}
    assert acme.status == "opened" and acme.press_release_url


def test_official_domain_rules():
    assert is_official_domain("https://www.prnewswire.com/news-releases/x", "acme")
    assert is_official_domain("https://news.acme.com/gcc", "acme")
    assert is_official_domain("https://pib.gov.in/x", "whatever")
    assert not is_official_domain("https://gcc.economictimes.indiatimes.com/news/x", "acme")


def test_precheck(settings):
    assert precheck(make_gcc(), TODAY, settings) is None
    assert "outside" in precheck(make_gcc(days_ago=30), TODAY, settings)
    assert precheck(make_gcc(event_date=None), TODAY, settings) == "no event date"
    assert precheck(make_gcc(city=""), TODAY, settings) == "no city stated"
    settings.include_announced = False
    assert "announced" in precheck(make_gcc(status="announced"), TODAY, settings)


# ---- press-release verification (network mocked) ----

class _Resp:
    def __init__(self, status=200, text=""):
        self.status_code, self.text = status, text


def _fetch(monkeypatch, status=200, text=""):
    from gcc_agents import evaluate_agent
    monkeypatch.setattr(evaluate_agent.httpx, "get", lambda *a, **k: _Resp(status, text))
    return evaluate_agent.verify_url


PAGE = "<html><body>Woodside Energy opens Bengaluru global capability centre in India.</body></html>"


def test_verify_url_accepts_real_announcement(monkeypatch):
    verify = _fetch(monkeypatch, 200, PAGE)
    assert verify("https://www.woodside.com/media/news/bengaluru", "woodside energy", "bengaluru")[0]


def test_verify_url_rejects_careers_and_social_and_offtopic(monkeypatch):
    verify = _fetch(monkeypatch, 200, PAGE)
    assert not verify("https://www.woodside.com/careers/current-vacancies/india", "woodside energy", "bengaluru")[0]
    assert not verify("https://www.woodside.com/contact", "woodside energy", "bengaluru")[0]
    assert not verify("https://www.linkedin.com/posts/woodside-energy_x", "woodside energy", "bengaluru")[0]
    off = _fetch(monkeypatch, 200, "<p>Woodside Energy annual report, Perth.</p>")
    assert not off("https://www.woodside.com/media/report", "woodside energy", "bengaluru")[0]


def test_verify_url_blocked_page_needs_news_like_path(monkeypatch):
    verify = _fetch(monkeypatch, 403)
    assert verify("https://www.woodside.com/media/news/woodside-opens-bengaluru-gcc", "woodside energy", "bengaluru")[0]
    assert not verify("https://www.woodside.com/media-centre/announcements", "woodside energy", "bengaluru")[0]
    assert not verify("https://www.woodside.com/energy", "woodside energy", "bengaluru")[0]


def test_social_hosts_are_never_official():
    assert not is_official_domain("https://www.linkedin.com/posts/the-kraft-heinz-company_x", "kraft heinz")
    assert not is_official_domain("https://x.com/kraftheinz/status/1", "kraft heinz")


def test_parse_web_date():
    from gcc_agents.dates import parse_web_date
    assert parse_web_date("2 days ago", TODAY) == date(2026, 9, 17)
    assert parse_web_date("5 hours ago", TODAY) == TODAY
    assert parse_web_date("Sep 18, 2026", TODAY) == date(2026, 9, 18)
    assert parse_web_date("1 week ago", TODAY) == date(2026, 9, 12)
    assert parse_web_date("", TODAY) is None and parse_web_date("soon", TODAY) is None


def test_fill_missing_dates_only_touches_undated_items():
    from gcc_agents.llm import fill_missing_dates
    undated, dated = make_gcc(event_date=None), make_gcc(company="B", event_date=date(2026, 9, 1))
    out = fill_missing_dates([undated, dated], date(2026, 9, 17))
    assert out[0].event_date == date(2026, 9, 17) and out[1].event_date == date(2026, 9, 1)
    assert fill_missing_dates([undated], None)[0].event_date is None


def test_accept_source_requires_llm_confirmation(monkeypatch, settings):
    from gcc_agents import evaluate_agent as ev
    item = make_gcc(company="Woodside Energy", city="Bengaluru")
    monkeypatch.setattr(ev, "verify_url", lambda *a, **k: (True, "generic announcements index page"))
    monkeypatch.setattr(ev, "confirm_announcement", lambda s, i, t: False)
    assert ev.accept_source(settings, item, "https://woodside.com/media-centre/announcements") is None
    monkeypatch.setattr(ev, "confirm_announcement", lambda s, i, t: True)
    assert ev.accept_source(settings, item, "https://woodside.com/media/x/") == "https://woodside.com/media/x"


def test_accept_source_blocked_page_skips_llm(monkeypatch, settings):
    from gcc_agents import evaluate_agent as ev
    monkeypatch.setattr(ev, "verify_url", lambda *a, **k: (True, ev.BLOCKED_NOTE))
    monkeypatch.setattr(ev, "confirm_announcement", lambda *a: (_ for _ in ()).throw(AssertionError("no LLM call")))
    assert ev.accept_source(settings, make_gcc(), "https://acme.com/news/x")


def test_contact_and_locations_paths_rejected(monkeypatch):
    verify = _fetch(monkeypatch, 200, PAGE)
    for path in ("/lets-talk", "/about-us/locations", "/contact-us", "/careers/india"):
        assert not verify(f"https://www.woodside.com{path}", "woodside energy", "bengaluru")[0], path


def test_is_company_post():
    from gcc_agents.evaluate_agent import is_company_post
    assert is_company_post("https://www.linkedin.com/posts/the-kraft-heinz-company_kraftheinzindia-activity-1", "kraft heinz")
    assert is_company_post("https://www.linkedin.com/company/woodside-energy/posts", "woodside energy")
    assert is_company_post("https://x.com/KraftHeinzCo/status/123", "kraft heinz")
    # someone else's post that merely mentions the company is NOT a company post
    assert not is_company_post("https://www.linkedin.com/posts/gcc-one-platform_woodsideenergy-bengaluru-activity-9", "woodside energy")
    assert not is_company_post("https://www.linkedin.com/posts/sameersinghal_welcome-to-woodside-india-activity-1", "woodside energy")
    assert not is_company_post("https://www.economictimes.com/news/kraft-heinz-1", "kraft heinz")


def test_fallback_candidates_order_and_junk_filtering():
    from gcc_agents.evaluate_agent import fallback_candidates
    item = make_gcc(company="Riveron", press_release_url="", source_urls=[])
    hits = [
        {"link": "https://www.glassdoor.co.in/Reviews/Riveron-1.htm", "title": "Riveron reviews", "snippet": ""},
        {"link": "https://www.bwpeople.in/article/riveron-expands-india-gcc-with-bengaluru-office-624363",
         "title": "Riveron expands India GCC", "snippet": "Riveron opens Bengaluru"},
    ]
    assert [(u, k) for u, k, _ in fallback_candidates(item, hits)] == [(hits[1]["link"], "news")]
    assert fallback_candidates(item, hits[:1]) == []
    hits.append({"link": "https://www.linkedin.com/posts/riveron_gcc-bengaluru-activity-5", "title": "", "snippet": ""})
    assert fallback_candidates(item, hits)[0][1] == "company_post"


def test_choose_fallback_skips_irrelevant_news(monkeypatch, settings):
    from gcc_agents import evaluate_agent as ev
    good = "https://www.themanufacturer.com/articles/hexadex-launches-pune-gcc-around-teconnex-99999"
    item = make_gcc(company="Hexadex", press_release_url="", source_urls=[
        "https://www.konsulteer.com/article/ses-expands-chennai-gcc-and-more-1234567",  # another company's article
        good,
    ])
    monkeypatch.setattr(ev, "news_is_relevant", lambda s, i, url, blurb="": "hexadex" in url)
    assert ev.choose_fallback(settings, item, []) == (good, "news")
    none_ok = make_gcc(company="Hexadex", press_release_url="",
                       source_urls=["https://x.example/news/other-company-story-12345"])
    monkeypatch.setattr(ev, "news_is_relevant", lambda *a, **k: False)
    assert ev.choose_fallback(settings, none_ok, []) == ("", "")


def test_news_is_relevant_uses_page_content_and_hints(monkeypatch, settings):
    from gcc_agents import evaluate_agent as ev
    item = make_gcc(company="Hexadex", city="Pune")
    monkeypatch.setattr(ev.httpx, "get", lambda *a, **k: _Resp(200, "<p>SES expands its Chennai GCC</p>"))
    monkeypatch.setattr(ev, "confirm_announcement", lambda s, i, text: "hexadex" in text.casefold())
    assert not ev.news_is_relevant(settings, item, "https://n.example/a-b-c-d-12345")
    monkeypatch.setattr(ev.httpx, "get", lambda *a, **k: _Resp(200, "<p>Hexadex launches Pune GCC</p>"))
    assert ev.news_is_relevant(settings, item, "https://n.example/a-b-c-d-12345")
    monkeypatch.setattr(ev.httpx, "get", lambda *a, **k: _Resp(403))  # unreadable: only strong hints pass
    assert not ev.news_is_relevant(settings, item, "https://n.example/some-story-12345")
    assert ev.news_is_relevant(settings, item, "https://n.example/hexadex-opens-pune-gcc-12345")


def test_stale_new_gcc_is_rejected_when_an_older_announcement_exists(settings):
    from gcc_agents import db, evaluate_agent as ev
    older = make_gcc(company="Syneos Health", city="Hyderabad", days_ago=25, discovery_sources=["gccindex"])
    late_article = make_gcc(company="Syneos Health", city="Hyderabad", days_ago=0, discovery_sources=["serper"])
    with db.connect() as conn:
        approved, rejected, _ = ev.run(settings, conn, [older, late_article], TODAY)
    assert not approved
    assert any("stale" in r["reason"] for r in rejected)
