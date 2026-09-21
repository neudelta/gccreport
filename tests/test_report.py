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
    assert "1 new GCC, 1 expansion" in rep.subject
    assert rep.chart_png.startswith(PNG) and rep.chart_cid == "sector_pie"
    assert "cid:sector_pie" in rep.html
    assert "Acme Corp" in rep.html and "Beta Bank" in rep.html and 'href="https://a.com/1"' in rep.html
    assert "2 item(s) found this week were already reported" in rep.html
    for gone in ("Sources checked", "Sources with issues", "best available source", "moneycontrol", "gccindex"):
        assert gone not in rep.html and gone not in rep.text
    assert "Acme Corp" in rep.text


def test_report_empty_week(settings, monkeypatch):
    rep = _build(settings, [], dups=1, monkeypatch=monkeypatch)
    assert "no new GCC activity" in rep.subject
    assert rep.chart_png is None and "cid:" not in rep.html
    assert "No new GCC launches or expansions" in rep.html


def test_html_is_escaped(settings, monkeypatch):
    rep = _build(settings, [make_gcc(company="<script>alert(1)</script>")], monkeypatch=monkeypatch)
    assert "<script>alert(1)</script>" not in rep.html


def test_mobile_friendly_card_layout(settings, monkeypatch):
    opened = make_gcc(press_release_url="https://a.com/1", state="Karnataka", sector="Energy")
    announced = make_gcc(company="Beta", city="Pune", state="", status="announced", press_release_url="https://b.com/1")
    html = _build(settings, [opened, announced], monkeypatch=monkeypatch).html
    # No side-by-side data columns that can collide on a phone, and a fluid container.
    assert "<th" not in html and "table-layout" not in html
    assert 'name="viewport"' in html and "max-width:720px" in html and "@media only screen and (max-width: 480px)" in html
    assert 'width="680"' not in html and 'width="100%"' in html
    # Each item is a labelled card: Location (city, state + sector under it), Date, Source.
    for label in (">Location<", ">Date<", ">Source<"):
        assert html.count(label) == 2, label
    assert "Bengaluru, Karnataka" in html and "Energy" in html
    assert ">Opened<" not in html and ">Announced<" in html               # only the non-default status is shown
    assert 'src="cid:sector_pie"' in html and "max-width:360px" in html   # chart shrinks with the screen
    assert "already reported" not in html


def test_source_host_is_short_and_only_for_news_and_posts():
    news = make_gcc(source_kind="news", source_url="https://gcc.economictimes.indiatimes.com/news/x/123456")
    uk = make_gcc(source_kind="news", source_url="https://www.bbc.co.uk/news/a-b-c-d")
    assert news.source_label == "News" and news.source_host == "indiatimes.com"
    assert uk.source_host == "bbc.co.uk"
    assert make_gcc(source_kind="press_release").source_host == ""
    assert make_gcc(source_kind="company_post", source_url="https://www.linkedin.com/posts/x").source_host == ""
