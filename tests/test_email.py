from unittest import mock

from gcc_agents import email_agent
from gcc_agents.report_agent import Report


def _report(chart=True):
    return Report(subject="Subj", html="<p>hi <img src='cid:sector_pie'></p>", text="hi",
                  chart_png=b"\x89PNG\r\n\x1a\nxx" if chart else None, chart_cid="sector_pie" if chart else None)


def test_message_structure(settings):
    msg = email_agent.build_message(settings, _report())
    assert msg["To"] == "me@example.com" and msg["Subject"] == "Subj"
    types = [p.get_content_type() for p in msg.walk()]
    assert types[:3] == ["multipart/related", "multipart/alternative", "text/plain"]
    assert "text/html" in types and "image/png" in types
    img = [p for p in msg.walk() if p.get_content_type() == "image/png"][0]
    assert img["Content-ID"] == "<sector_pie>"


def test_message_without_chart(settings):
    types = [p.get_content_type() for p in email_agent.build_message(settings, _report(chart=False)).walk()]
    assert "image/png" not in types


def test_send_success_strips_spaces_from_app_password(settings):
    with mock.patch("gcc_agents.email_agent.smtplib.SMTP_SSL") as smtp:
        ok, detail = email_agent.send(settings, _report())
    assert ok and detail == "sent"
    smtp.return_value.__enter__.return_value.login.assert_called_once_with("me@example.com", "apppassword")


def test_send_retries_then_fails(settings):
    with mock.patch("gcc_agents.email_agent.smtplib.SMTP_SSL", side_effect=OSError("boom")) as smtp, \
            mock.patch("gcc_agents.email_agent.time.sleep"):
        ok, detail = email_agent.send(settings, _report())
    assert not ok and "OSError" in detail and smtp.call_count == 2


def test_send_reports_missing_config(settings):
    settings.to_email = ""
    ok, detail = email_agent.send(settings, _report())
    assert not ok and "TO_EMAIL" in detail


def test_message_with_pdf_and_hero_image(settings):
    png = b"\x89PNG\r\n\x1a\nxx"
    rep = Report(subject="S", html="<img src='cid:ltm_hero'><img src='cid:sector_pie'>", text="t",
                 chart_png=png, chart_cid="sector_pie", images={"ltm_hero": png},
                 pdf=b"%PDF-1.7 x", pdf_name="LTM_India_GCC_Briefing_2026-11-02.pdf")
    msg = email_agent.build_message(settings, rep)
    types = [p.get_content_type() for p in msg.walk()]
    assert types[:3] == ["multipart/mixed", "multipart/related", "multipart/alternative"]
    cids = {p["Content-ID"] for p in msg.walk() if p.get_content_type() == "image/png"}
    assert cids == {"<ltm_hero>", "<sector_pie>"}
    pdf = [p for p in msg.walk() if p.get_content_type() == "application/pdf"][0]
    assert pdf.get_filename() == "LTM_India_GCC_Briefing_2026-11-02.pdf" and msg["To"] == "me@example.com"
