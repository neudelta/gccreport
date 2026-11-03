"""Dashboard API (FastAPI TestClient; email, AI and PDF are faked by conftest / here)."""
from datetime import date

import pytest
from conftest import FAKE_INSIGHT, make_gcc
from fastapi.testclient import TestClient

from gcc_agents import db

H = {"X-GCC-Dashboard": "1"}


@pytest.fixture
def client(monkeypatch, settings):
    from dashboard import app as dash
    monkeypatch.setattr(dash.config, "get_settings", lambda: settings)
    with db.connect() as conn:
        db.save_catalog(conn, [
            make_gcc(company="Havells India", city="Noida", sector="Manufacturing/Auto", event_date=date(2026, 10, 27),
                     source_url="", source_kind="news", press_release_url="", summary="UP cabinet cleared its GCC."),
            make_gcc(company="TIAA", city="Pune", sector="BFSI", event_type="expansion", event_date=date(2026, 10, 23),
                     press_release_url="https://p.example/tiaa", insight=FAKE_INSIGHT),
            make_gcc(company="Acme Corp", city="Bengaluru", event_date=date(2026, 3, 4),
                     press_release_url="https://a.example/1"),
        ])
    return TestClient(dash.app)


def test_page_and_facets(client):
    assert "LTM GCC Radar" in client.get("/").text
    f = client.get("/api/facets").json()
    assert f["total"] == 3 and f["months"] == ["2026-10", "2026-03"]
    assert set(f["cities"]) == {"Noida", "Pune", "Bengaluru"} and f["min_date"] == "2026-03-04"  # canonical names
    assert client.get("/hero.png").headers["content-type"] == "image/png"


@pytest.mark.parametrize("query,expected", [
    ("", {"Havells India", "TIAA", "Acme Corp"}),
    ("start=2026-10-01&end=2026-10-31", {"Havells India", "TIAA"}),
    ("month=2026-03", {"Acme Corp"}),
    ("city=Bangalore", {"Acme Corp"}),                       # city aliases work
    ("sector=BFSI", {"TIAA"}),
    ("type=expansion", {"TIAA"}),
    ("fit=High", {"TIAA"}),
    ("fit=none", {"Havells India", "Acme Corp"}),
    ("q=cabinet", {"Havells India"}),                        # searches the summary
    ("q=oil and gas", {"TIAA"}),                             # ... and the LTM analysis
    ("month=2026-10&city=Pune", {"TIAA"}),                   # filters combine
])
def test_filters_and_search(client, query, expected):
    d = client.get("/api/gccs?" + query).json()
    assert {i["company"] for i in d["items"]} == expected
    assert d["kpis"]["total"] == len(expected)


def test_kpis_and_charts(client):
    d = client.get("/api/gccs").json()
    assert d["kpis"] == {"total": 3, "new": 2, "expansion": 1, "cities": 3, "high": 1, "analysed": 1}
    assert d["charts"]["by_month"] == [["2026-03", 1], ["2026-10", 2]]


def test_bad_filters_are_rejected(client):
    assert client.get("/api/gccs?start=yesterday").status_code == 400
    assert client.get("/api/gccs?type=rumour").status_code == 400


def test_row_without_public_link_shows_gcc_index(client):
    havells = [i for i in client.get("/api/gccs").json()["items"] if i["company"] == "Havells India"][0]
    assert havells["source_url"] == "" and havells["source_label"] == "GCC Index"


def test_state_changing_calls_need_the_dashboard_header(client):
    gid = client.get("/api/gccs?fit=none").json()["items"][0]["id"]
    assert client.post(f"/api/gcc/{gid}/analyse").status_code == 403
    assert client.post("/api/email", json={"recipients": "a@b.com"}).status_code == 403


def test_analyse_saves_the_insight(client):
    gid = [i for i in client.get("/api/gccs").json()["items"] if i["company"] == "Acme Corp"][0]["id"]
    r = client.post(f"/api/gcc/{gid}/analyse", headers=H).json()
    assert r["fit"] == "High" and r["insight"]["engagement_model"] == "Build-Operate-Transform-Transfer"
    assert client.get(f"/api/gcc/{gid}").json()["fit"] == "High"     # stored
    assert client.get("/api/gcc/99999").status_code == 404


def test_csv_and_pdf_downloads(client):
    csv = client.get("/api/export.csv?month=2026-10").text
    assert csv.startswith("﻿date,company") and "Havells India" in csv and "Acme Corp" not in csv
    r = client.get("/api/report.pdf?month=2026-10")
    assert r.status_code == 200 and r.content == b"%PDF-1.7 fake"
    assert "attachment" in r.headers["content-disposition"]
    assert client.get("/api/report.pdf?q=nothing-matches").status_code == 404
    # building the report analysed the un-analysed GCC in the selection, and saved it
    assert {i["company"] for i in client.get("/api/gccs?fit=none").json()["items"]} == {"Acme Corp"}


def test_email_sends_to_the_given_recipients_and_saves_them(client, monkeypatch):
    from dashboard import app as dash
    sent = {}
    monkeypatch.setattr(dash.email_agent, "send", lambda s, rep, recipients="": sent.update(to=recipients, rep=rep) or (True, "sent"))
    body = {"filters": {"month": "2026-10"}, "recipients": "ceo@ltm.com; cso@ltm.com, ceo@ltm.com", "save_default": True}
    r = client.post("/api/email", headers=H, json=body)
    assert r.status_code == 200 and r.json()["recipients"] == ["ceo@ltm.com", "cso@ltm.com"]
    assert sent["to"] == "ceo@ltm.com, cso@ltm.com" and "Havells India" in sent["rep"].html
    assert client.get("/api/recipients").json()["recipients"] == "ceo@ltm.com, cso@ltm.com"


def test_email_validation_and_failure(client, monkeypatch):
    from dashboard import app as dash
    assert client.post("/api/email", headers=H, json={"recipients": "not-an-email"}).status_code == 400
    assert client.post("/api/email", headers=H, json={"recipients": "  "}).status_code == 400
    monkeypatch.setattr(dash.email_agent, "send", lambda *a, **k: (False, "smtp down"))
    r = client.post("/api/email", headers=H, json={"recipients": "a@ltm.com"})
    assert r.status_code == 502 and "smtp down" in r.json()["detail"]
    assert client.get("/api/recipients").json()["recipients"] != "a@ltm.com"   # not saved on failure


def test_port_check_explains_a_busy_port():
    import socket

    import main
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as srv:
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        port = srv.getsockname()[1]
        msg = main.port_problem(port)                 # something that is not the dashboard holds the port
        assert f"Port {port} is used by another program" in msg and f"--port {port + 80}" in msg
    assert main.port_problem(port) == ""              # free again



def test_reports_cover_the_full_filtered_list(client, monkeypatch):
    """No caps: every GCC in the filter is analysed and listed in the email and the PDF profiles."""
    from dashboard import app as dash
    with db.connect() as conn:
        db.save_catalog(conn, [make_gcc(company=f"Firm{n:02d}", city="Chennai", event_date=date(2026, 5, n % 28 + 1),
                                        press_release_url=f"https://f.example/{n}") for n in range(45)])
    sent = {}
    monkeypatch.setattr(dash.email_agent, "send", lambda s, rep, recipients="": sent.update(rep=rep) or (True, "sent"))
    r = client.post("/api/email", headers=H, json={"filters": {"month": "2026-05"}, "recipients": "a@ltm.com"})
    assert r.status_code == 200
    rep = sent["rep"]
    assert all(f"Firm{n:02d}" in rep.html for n in range(45)) and "more in the attached" not in rep.html
    assert rep.pdf_html.count('class="acct"') == 45                      # a profile for every GCC
    assert 'class="co"' in rep.html                                      # dashboard emails use the slim rows
    assert client.get("/api/gccs?month=2026-05&fit=none").json()["kpis"]["total"] == 0   # all analysed and saved
