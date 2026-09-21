"""SQLite persistence: every reported GCC event plus one row per report run."""
import csv
import json
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Iterable, Optional

from . import config
from .models import GCC, canonical_url, normalize_company

SCHEMA = """
CREATE TABLE IF NOT EXISTS reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_at TEXT NOT NULL,
    window_start TEXT NOT NULL,
    window_end TEXT NOT NULL,
    new_count INTEGER NOT NULL DEFAULT 0,
    expansion_count INTEGER NOT NULL DEFAULT 0,
    email_status TEXT NOT NULL,
    sent_at TEXT,
    html_path TEXT,
    source_status TEXT
);
CREATE TABLE IF NOT EXISTS gcc (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    company TEXT NOT NULL,
    company_key TEXT NOT NULL,
    city TEXT NOT NULL,
    city_key TEXT NOT NULL,
    state TEXT,
    sector TEXT,
    event_type TEXT NOT NULL CHECK (event_type IN ('new_gcc', 'expansion')),
    status TEXT NOT NULL,
    event_date TEXT,
    headcount TEXT,
    summary TEXT,
    press_release_url TEXT NOT NULL DEFAULT '',
    source_url TEXT NOT NULL DEFAULT '',
    source_kind TEXT NOT NULL DEFAULT 'press_release',
    source_urls TEXT,
    discovery_sources TEXT,
    event_key TEXT NOT NULL UNIQUE,
    first_reported_at TEXT NOT NULL,
    report_id INTEGER REFERENCES reports(id)
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_gcc_press_release ON gcc(press_release_url) WHERE press_release_url <> '';
CREATE INDEX IF NOT EXISTS ix_gcc_company ON gcc(company_key);
CREATE INDEX IF NOT EXISTS ix_gcc_event_date ON gcc(event_date);
"""


def _migrate(conn: sqlite3.Connection) -> None:
    """Upgrade databases created before source_url / source_kind existed."""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(gcc)")}
    if "source_url" not in cols:
        conn.execute("ALTER TABLE gcc ADD COLUMN source_url TEXT NOT NULL DEFAULT ''")
        conn.execute("UPDATE gcc SET source_url = press_release_url")
    if "source_kind" not in cols:
        conn.execute("ALTER TABLE gcc ADD COLUMN source_kind TEXT NOT NULL DEFAULT 'press_release'")


@contextmanager
def connect(path: Optional[Path] = None):
    path = path or config.DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    _migrate(conn)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def is_duplicate(conn: sqlite3.Connection, item: GCC) -> bool:
    if conn.execute("SELECT 1 FROM gcc WHERE event_key = ?", (item.event_key,)).fetchone():
        return True
    url = canonical_url(item.press_release_url)
    return bool(url and conn.execute("SELECT 1 FROM gcc WHERE press_release_url = ?", (url,)).fetchone())


def prior_for_company(conn: sqlite3.Connection, company: str) -> list[dict]:
    key = normalize_company(company)
    rows = conn.execute(
        "SELECT company, city, event_type, event_date, summary FROM gcc WHERE company_key = ? ORDER BY event_date DESC",
        (key,),
    ).fetchall()
    return [dict(r) for r in rows]


def reported_summary(conn: sqlite3.Connection, limit: int = 200) -> list[str]:
    """Compact 'Company (City, type)' strings fed to search/extraction prompts as an exclusion list."""
    rows = conn.execute(
        "SELECT company, city, event_type FROM gcc ORDER BY first_reported_at DESC LIMIT ?", (limit,)
    ).fetchall()
    return [f"{r['company']} ({r['city']}, {r['event_type']})" for r in rows]


def save_report(
    conn: sqlite3.Connection,
    *,
    window_start: date,
    window_end: date,
    items: Iterable[GCC],
    email_status: str,
    sent_at: Optional[datetime],
    html_path: Optional[str],
    source_status: dict,
) -> tuple[int, int]:
    """Insert one report row + its items. Returns (report_id, items_saved)."""
    items = list(items)
    now = datetime.now().isoformat(timespec="seconds")
    cur = conn.execute(
        "INSERT INTO reports (run_at, window_start, window_end, new_count, expansion_count, email_status,"
        " sent_at, html_path, source_status) VALUES (?,?,?,?,?,?,?,?,?)",
        (
            now, window_start.isoformat(), window_end.isoformat(),
            sum(i.event_type == "new_gcc" for i in items),
            sum(i.event_type == "expansion" for i in items),
            email_status, sent_at.isoformat(timespec="seconds") if sent_at else None,
            html_path, json.dumps(source_status),
        ),
    )
    report_id = cur.lastrowid
    saved = 0
    for it in items:
        try:
            conn.execute(
                "INSERT INTO gcc (company, company_key, city, city_key, state, sector, event_type, status,"
                " event_date, headcount, summary, press_release_url, source_url, source_kind, source_urls,"
                " discovery_sources, event_key, first_reported_at, report_id)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    it.company, it.company_key, it.city, it.city_key, it.state, it.sector, it.event_type,
                    it.status, it.event_date.isoformat() if it.event_date else None, it.headcount, it.summary,
                    canonical_url(it.press_release_url), it.source_url, it.source_kind,
                    json.dumps(it.source_urls), json.dumps(it.discovery_sources), it.event_key, now, report_id,
                ),
            )
            saved += 1
        except sqlite3.IntegrityError:
            continue  # concurrent duplicate; the UNIQUE constraints are the hard guarantee
    return report_id, saved


def list_items(
    conn: sqlite3.Connection,
    year: Optional[int] = None,
    sector: Optional[str] = None,
    city: Optional[str] = None,
    event_type: Optional[str] = None,
) -> list[dict]:
    sql, args = "SELECT * FROM gcc WHERE 1=1", []
    if year:
        sql += " AND substr(COALESCE(event_date, first_reported_at), 1, 4) = ?"
        args.append(str(year))
    if sector:
        sql += " AND lower(sector) = lower(?)"
        args.append(sector)
    if city:
        sql += " AND city_key = ?"
        from .models import canonical_city
        args.append(canonical_city(city))
    if event_type:
        sql += " AND event_type = ?"
        args.append(event_type)
    sql += " ORDER BY COALESCE(event_date, first_reported_at) DESC"
    return [dict(r) for r in conn.execute(sql, args).fetchall()]


def stats(conn: sqlite3.Connection, year: Optional[int] = None) -> dict:
    rows = list_items(conn, year=year)
    out: dict = {"total": len(rows), "by_type": {}, "by_sector": {}, "by_city": {}, "by_month": {}}
    for r in rows:
        out["by_type"][r["event_type"]] = out["by_type"].get(r["event_type"], 0) + 1
        out["by_sector"][r["sector"]] = out["by_sector"].get(r["sector"], 0) + 1
        out["by_city"][r["city"]] = out["by_city"].get(r["city"], 0) + 1
        month = (r["event_date"] or r["first_reported_at"])[:7]
        out["by_month"][month] = out["by_month"].get(month, 0) + 1
    return out


def export_csv(conn: sqlite3.Connection, path: Path, year: Optional[int] = None) -> int:
    rows = list_items(conn, year=year)
    cols = ["event_date", "company", "city", "state", "sector", "event_type", "status", "headcount",
            "summary", "source_kind", "source_url", "press_release_url", "discovery_sources",
            "first_reported_at"]
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    return len(rows)


def reset(conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM gcc")
    conn.execute("DELETE FROM reports")


def load_report(conn: sqlite3.Connection, report_id: Optional[int] = None) -> Optional[tuple[dict, list[GCC]]]:
    """A stored report row and its items (the last successfully sent report if no id is given)."""
    if report_id is None:
        row = conn.execute("SELECT * FROM reports WHERE email_status = 'sent' ORDER BY id DESC LIMIT 1").fetchone()
    else:
        row = conn.execute("SELECT * FROM reports WHERE id = ?", (report_id,)).fetchone()
    if row is None:
        return None
    items = []
    for r in conn.execute("SELECT * FROM gcc WHERE report_id = ? ORDER BY id", (row["id"],)):
        items.append(GCC(
            company=r["company"], city=r["city"], state=r["state"] or "", sector=r["sector"] or "Other",
            event_type=r["event_type"], status=r["status"],
            event_date=date.fromisoformat(r["event_date"]) if r["event_date"] else None,
            headcount=r["headcount"] or "", summary=r["summary"] or "",
            press_release_url=r["press_release_url"] or "", source_url=r["source_url"] or "",
            source_kind=r["source_kind"] or "news", source_urls=json.loads(r["source_urls"] or "[]"),
            discovery_sources=json.loads(r["discovery_sources"] or "[]"),
        ))
    return dict(row), items
