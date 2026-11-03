from datetime import timedelta

from conftest import TODAY, make_gcc

from gcc_agents import report_agent

PNG = b"\x89PNG\r\n\x1a\n"


def _build(settings, items, dups=0, monkeypatch=None):
    monkeypatch.setattr(report_agent, "executive_summary", lambda s, i: "Summary text.")
    return report_agent.build(settings, items, TODAY - timedelta(days=7), TODAY, dups,
                              {"serper": "ok: 3", "moneycontrol": "blocked: Access Denied", "gccindex": "skipped: x"})


def test_report_with_items(settings, monkeypatch):
    items = [
        make_gcc(press_release_url="https://a.com/1"),
        make_gcc(company="Beta Bank", city="Hyderabad", sector="BFSI", event_type="expansion",
                 press_release_url="https://b.com/1"),
    ]
    rep = _build(settings, items, dups=2, monkeypatch=monkeypatch)
    assert rep.subject.startswith("India GCC Report | 1 new GCC · 1 expansion")
    assert rep.chart_png.startswith(PNG) and rep.chart_cid == "sector_pie"
    assert "cid:sector_pie" in rep.html
    assert "Acme Corp" in rep.html and "Beta Bank" in rep.html
    assert 'href="https://a.com/1"' in rep.pdf_html                        # source links live in the briefing
    assert "2 item(s) seen this period were already in an earlier brief" in rep.html
    for gone in ("Sources checked", "Sources with issues", "best available source", "moneycontrol", "gccindex"):
        assert gone not in rep.html and gone not in rep.text
    assert "Acme Corp" in rep.text


def test_report_empty_week(settings, monkeypatch):
    rep = _build(settings, [], dups=1, monkeypatch=monkeypatch)
    assert rep.subject.startswith("India GCC Report | no new GCC activity")
    assert rep.chart_png is None and "cid:sector_pie" not in rep.html  # the LTM hero is always there
    assert "No new GCC launches or expansions" in rep.html


def test_html_is_escaped(settings, monkeypatch):
    rep = _build(settings, [make_gcc(company="<script>alert(1)</script>")], monkeypatch=monkeypatch)
    assert "<script>alert(1)</script>" not in rep.html


def test_ltm_email_overview_table_and_briefing(settings, monkeypatch):
    from conftest import FAKE_INSIGHT
    hi = make_gcc(company="Woodside Energy", sector="Energy", insight=FAKE_INSIGHT)
    lo = make_gcc(company="Beta", city="Pune", state="", status="announced", event_type="expansion",
                  press_release_url="https://b.com/1", insight=FAKE_INSIGHT.model_copy(update={"fit": "Low"}))
    rep = _build(settings, [lo, hi], monkeypatch=monkeypatch)
    html = rep.html
    # LTM theme and a phone-safe overview table (4 short columns, sector hidden on small screens)
    assert ">LTM<" in html and "GCC Origin" in html and "#F2553D" in html
    assert ">India GCC Report</div>" in html and "Opportunity Brief" not in html   # headline matches the subject
    assert html.count("<th ") == 4 and 'class="hide-sm"' in html and "max-width:720px" in html
    assert "@media only screen and (max-width: 520px)" in html
    assert html.index("Woodside Energy") < html.index(">Beta<")           # high fit is listed first
    assert "Where GCC Origin can engage first" in html and "Build-Operate-Transform-Transfer" in html
    assert "cid:ltm_hero" in html and "cid:sector_pie" in html and "India GCC Weekly Intel Report attached" in html
    assert "announced" in html                                            # announced items are flagged
    # detailed briefing: business area, products, India-centre mandate and the LTM play
    assert rep.pdf == b"%PDF-1.7 fake" and rep.pdf_name.startswith("India GCC Weekly Intel Report")
    assert rep.pdf_name.endswith(".pdf") and "India GCC<br>Weekly Intel Report" in rep.pdf_html
    for text in ("Business area", "Products &amp; services", "The India centre", "How LTM wins",
                 "Oil and gas producer", "LNG", "Next step", "LTM GCC Origin at a glance"):
        assert text in rep.pdf_html, text
    assert set(rep.images) == {"ltm_hero"} and rep.images["ltm_hero"].startswith(PNG)


def test_no_pdf_for_an_empty_week(settings, monkeypatch):
    rep = _build(settings, [], monkeypatch=monkeypatch)
    assert rep.pdf is None and "Intel Report attached" not in rep.html


def test_source_host_is_short_and_only_for_news_and_posts():
    news = make_gcc(source_kind="news", source_url="https://gcc.economictimes.indiatimes.com/news/x/123456")
    uk = make_gcc(source_kind="news", source_url="https://www.bbc.co.uk/news/a-b-c-d")
    assert news.source_label == "News" and news.source_host == "indiatimes.com"
    assert uk.source_host == "bbc.co.uk"
    assert make_gcc(source_kind="press_release").source_host == ""
    assert make_gcc(source_kind="company_post", source_url="https://www.linkedin.com/posts/x").source_host == ""
