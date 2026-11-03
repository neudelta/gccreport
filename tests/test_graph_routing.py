"""End-to-end graph behaviour with every network/LLM call mocked."""
from unittest import mock

import pytest
from conftest import TODAY, make_gcc

from gcc_agents import db, evaluate_agent, graph, report_agent
from gcc_agents.evaluate_agent import Verdict


@pytest.fixture
def wired(monkeypatch, settings):
    """Sources return whatever the test sets on `box`; judge approves; URLs verify; email is captured."""
    box = {"serper": ([], "ok: 0"), "x": ([], "ok: 0"), "sites": ([], {"et_gcc": "ok: 0"}), "retry_calls": 0}

    def serper_run(s, today, reported, attempt=0):
        if attempt == 1:
            box["retry_calls"] += 1
        return box["serper"] if attempt == 0 else ([], "ok: retry 0")

    monkeypatch.setattr(graph.serper_source, "run", serper_run)
    monkeypatch.setattr(graph.x_source, "run", lambda s, t, r: box["x"])
    monkeypatch.setattr(graph.sites_source, "run", lambda s, t, r: box["sites"])
    monkeypatch.setattr(
        evaluate_agent, "_judge",
        lambda s, item, prior: Verdict(in_scope=True, event_type=item.event_type, status=item.status,
                                       sector="Technology", same_as_prior=False, reason="ok"),
    )
    monkeypatch.setattr(evaluate_agent, "verify_url", lambda *a, **k: (True, "ok"))
    monkeypatch.setattr(evaluate_agent, "confirm_announcement", lambda *a, **k: True)
    monkeypatch.setattr(evaluate_agent, "news_is_relevant", lambda *a, **k: True)
    monkeypatch.setattr(evaluate_agent, "find_press_release", lambda s, i, fallbacks=None: None)
    monkeypatch.setattr(report_agent, "executive_summary", lambda s, i: "Summary.")
    sent = []
    monkeypatch.setattr(graph.email_agent, "send", lambda s, rep: (sent.append(rep) or True, "sent"))
    box["sent"] = sent
    return box


def run(settings, dry=False):
    return graph.run_once(settings, dry_run=dry, today=TODAY)


def test_approved_item_is_emailed_and_stored(wired, settings):
    wired["serper"] = ([make_gcc()], "ok: 1")
    final = run(settings)
    assert final["outcome"] == "sent" and len(wired["sent"]) == 1
    assert "1 new GCC" in wired["sent"][0].subject
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM gcc").fetchone()[0] == 1


def test_same_item_next_run_is_not_repeated_and_empty_week_email_is_sent(wired, settings):
    wired["serper"] = ([make_gcc()], "ok: 1")
    run(settings)
    wired["sent"].clear()
    final = run(settings)  # same candidate again
    assert final["outcome"] == "sent" and not final["approved"]
    assert final["already_reported_count"] == 1
    assert "no new GCC activity" in wired["sent"][0].subject


def test_zero_candidates_sends_empty_week_email_after_one_retry(wired, settings):
    final = run(settings)
    assert final["outcome"] == "sent" and wired["retry_calls"] == 1
    assert "no new GCC activity" in wired["sent"][0].subject
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM reports WHERE email_status='sent'").fetchone()[0] == 1


def test_empty_week_email_can_be_disabled(wired, settings):
    settings.send_empty_report = False
    final = run(settings)
    assert final["outcome"] == "nothing_to_send" and not wired["sent"]


def test_all_sources_failed_does_not_claim_nothing_new(wired, settings):
    wired["serper"] = ([], "failed: boom")
    wired["x"] = ([], "failed: boom")
    wired["sites"] = ([], {"et_gcc": "failed: boom", "moneycontrol": "blocked: x"})
    final = run(settings)
    assert final["outcome"] == "search_failed" and not wired["sent"]
    with db.connect() as conn:
        assert conn.execute("SELECT email_status FROM reports").fetchone()[0] == "search_failed"


def test_one_source_failing_still_produces_report(wired, settings):
    wired["serper"] = ([], "failed: boom")
    wired["sites"] = ([make_gcc(discovery_sources=["et_gcc"])], {"et_gcc": "ok: 1"})
    final = run(settings)
    assert final["outcome"] == "sent"
    assert "serper" not in wired["sent"][0].html  # source status is no longer shown in the email


def test_failed_send_does_not_burn_items(wired, settings, monkeypatch):
    wired["serper"] = ([make_gcc()], "ok: 1")
    monkeypatch.setattr(graph.email_agent, "send", lambda s, rep: (False, "smtp down"))
    final = run(settings)
    assert final["outcome"] == "send_failed"
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM gcc").fetchone()[0] == 0
    # next run (email works again) can still report it
    monkeypatch.setattr(graph.email_agent, "send", lambda s, rep: (True, "sent"))
    assert run(settings)["outcome"] == "sent"
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM gcc").fetchone()[0] == 1


def test_dry_run_writes_preview_and_no_db(wired, settings, tmp_data):
    wired["serper"] = ([make_gcc()], "ok: 1")
    final = run(settings, dry=True)
    assert final["outcome"] == "dry_run" and (tmp_data / "preview.html").exists()
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM gcc").fetchone()[0] == 0


def test_no_official_press_release_falls_back_to_news_and_is_not_rejected(wired, settings):
    item = make_gcc(press_release_url="", source_url="", source_kind="news",
                    source_urls=["https://www.businessstandard.com/companies/news/acme-opens-gcc-in-bengaluru-123456"])
    wired["serper"] = ([item], "ok: 1")
    final = run(settings)
    assert len(final["approved"]) == 1
    got = final["approved"][0]
    assert got.source_kind == "news" and got.source_url.startswith("https://www.businessstandard.com/")
    assert got.press_release_url == ""
    assert "News" in wired["sent"][0].pdf_html and "businessstandard.com" in wired["sent"][0].text


def test_company_linkedin_post_beats_news(wired, settings):
    item = make_gcc(company="Kraft Heinz", press_release_url="", source_url="", source_kind="news",
                    source_urls=["https://www.et.com/news/kraft-heinz-opens-bengaluru-hub-99999",
                                 "https://www.linkedin.com/posts/the-kraft-heinz-company_kraftheinzindia-activity-123"])
    wired["serper"] = ([item], "ok: 1")
    final = run(settings)
    assert final["approved"][0].source_kind == "company_post"


def test_item_with_no_link_at_all_is_rejected(wired, settings):
    wired["serper"] = ([make_gcc(press_release_url="", source_url="", source_urls=[])], "ok: 1")
    final = run(settings)
    assert not final["approved"]
    assert any("no source link" in r["reason"] for r in final["rejected"])
