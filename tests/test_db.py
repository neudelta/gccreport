import sqlite3
from datetime import datetime

import pytest
from conftest import TODAY, make_gcc

from gcc_agents import db


def _save(conn, items, status="sent"):
    return db.save_report(
        conn, window_start=TODAY, window_end=TODAY, items=items, email_status=status,
        sent_at=datetime.now(), html_path=None, source_status={"serper": "ok"},
    )


def test_duplicate_detection_by_event_key_and_url():
    with db.connect() as conn:
        _save(conn, [make_gcc()])
        assert db.is_duplicate(conn, make_gcc(company="ACME Inc", city="Bangalore"))
        assert db.is_duplicate(conn, make_gcc(company="Different", city="Pune"))  # same press-release URL
        assert not db.is_duplicate(conn, make_gcc(company="Different", city="Pune", press_release_url="https://d.com/x"))


def test_new_gcc_unique_forever_but_expansion_can_recur_next_month():
    from datetime import date

    with db.connect() as conn:
        _save(conn, [make_gcc(press_release_url="https://a.com/1")])
        exp_sep = make_gcc(event_type="expansion", event_date=date(2026, 9, 3), press_release_url="https://a.com/2")
        exp_oct = make_gcc(event_type="expansion", event_date=date(2026, 10, 3), press_release_url="https://a.com/3")
        _save(conn, [exp_sep])
        assert db.is_duplicate(conn, exp_sep)
        assert not db.is_duplicate(conn, exp_oct)


def test_unique_constraint_is_hard_guarantee():
    with db.connect() as conn:
        _, saved = _save(conn, [make_gcc(), make_gcc()])
        assert saved == 1
        raw = conn.execute("SELECT COUNT(*) FROM gcc").fetchone()[0]
        assert raw == 1


def test_empty_report_row_is_recorded():
    with db.connect() as conn:
        rid, saved = _save(conn, [])
        assert saved == 0
        row = conn.execute("SELECT new_count, expansion_count, email_status FROM reports WHERE id=?", (rid,)).fetchone()
        assert tuple(row) == (0, 0, "sent")


def test_list_stats_export(tmp_path):
    with db.connect() as conn:
        _save(conn, [
            make_gcc(press_release_url="https://a.com/1"),
            make_gcc(company="Beta", city="Hyderabad", sector="BFSI", event_type="expansion",
                     press_release_url="https://b.com/1"),
        ])
        assert len(db.list_items(conn, year=2026)) == 2
        assert [r["company"] for r in db.list_items(conn, sector="bfsi")] == ["Beta"]
        assert [r["company"] for r in db.list_items(conn, city="hyderabad")] == ["Beta"]
        s = db.stats(conn, 2026)
        assert s["total"] == 2 and s["by_type"] == {"new_gcc": 1, "expansion": 1}
        out = tmp_path / "x.csv"
        assert db.export_csv(conn, out, 2026) == 2
        assert "Beta" in out.read_text(encoding="utf-8")


def test_reset():
    with db.connect() as conn:
        _save(conn, [make_gcc()])
        db.reset(conn)
        assert conn.execute("SELECT COUNT(*) FROM gcc").fetchone()[0] == 0


def test_load_report_round_trips_items():
    with db.connect() as conn:
        original = make_gcc(source_url="https://news.example/story-1234567", source_kind="news")
        rid, _ = _save(conn, [original])
        row, items = db.load_report(conn)
        assert row["id"] == rid and len(items) == 1
        got = items[0]
        assert (got.company, got.city, got.event_type, got.event_date) == (
            original.company, original.city, original.event_type, original.event_date)
        assert got.source_url == original.source_url and got.source_kind == "news"
        assert db.load_report(conn, 999) is None


def test_insight_is_stored_loaded_and_refreshed():
    from conftest import FAKE_INSIGHT
    with db.connect() as conn:
        _save(conn, [make_gcc()])
        _, items = db.load_report(conn)
        assert items[0].insight is None
        assert db.save_insights(conn, [items[0].model_copy(update={"insight": FAKE_INSIGHT})]) == 1
        _, items = db.load_report(conn)
        assert items[0].insight == FAKE_INSIGHT
