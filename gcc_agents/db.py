"""SQLite persistence: every reported GCC event plus one row per report run."""
import csv
import json
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Iterable, Optional

from . import config
from .models import GCC, LTMInsight, canonical_url, normalize_company, same_event

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
    insight TEXT,
    origin TEXT NOT NULL DEFAULT 'weekly',
    source_urls TEXT,
    discovery_sources TEXT,
    event_key TEXT NOT NULL UNIQUE,
    first_reported_at TEXT NOT NULL,
    report_id INTEGER REFERENCES reports(id)
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_gcc_press_release ON gcc(press_release_url) WHERE press_release_url <> '';
CREATE INDEX IF NOT EXISTS ix_gcc_company ON gcc(company_key);
CREATE INDEX IF NOT EXISTS ix_gcc_event_date ON gcc(event_date);
CREATE TABLE IF NOT EXISTS app_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


def _migrate(conn: sqlite3.Connection) -> None:
    """Upgrade databases created before source_url / source_kind existed."""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(gcc)")}
    if "source_url" not in cols:
        conn.execute("ALTER TABLE gcc ADD COLUMN source_url TEXT NOT NULL DEFAULT ''")
        conn.execute("UPDATE gcc SET source_url = press_release_url")
    if "source_kind" not in cols:
        conn.execute("ALTER TABLE gcc ADD COLUMN source_kind TEXT NOT NULL DEFAULT 'press_release'")
    if "insight" not in cols:
        conn.execute("ALTER TABLE gcc ADD COLUMN insight TEXT")
    if "origin" not in cols:
        conn.execute("ALTER TABLE gcc ADD COLUMN origin TEXT NOT NULL DEFAULT 'weekly'")


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


def find_same_event(conn: sqlite3.Connection, item: GCC):
    """The stored row describing the same event as `item` (name variants and new-vs-expansion labels included)."""
    row = conn.execute("SELECT * FROM gcc WHERE event_key = ?", (item.event_key,)).fetchone()
    if row:
        return row
    url = canonical_url(item.press_release_url)
    if url:
        row = conn.execute("SELECT * FROM gcc WHERE press_release_url = ?", (url,)).fetchone()
        if row:
            return row
    for r in conn.execute("SELECT * FROM gcc WHERE city_key = ?", (item.city_key,)):
        d = date.fromisoformat(r["event_date"]) if r["event_date"] else None
        if same_event(item.company_key, item.city_key, item.event_type, item.event_date,
                      r["company_key"], r["city_key"], r["event_type"], d):
            return r
    return None


def is_duplicate(conn: sqlite3.Connection, item: GCC) -> bool:
    return find_same_event(conn, item) is not None


def is_reported(conn: sqlite3.Connection, item: GCC) -> bool:
    """Already emailed (or historical backfill) - as opposed to collected by the daily refresh and still pending."""
    row = find_same_event(conn, item)
    return bool(row and (row["report_id"] is not None or row["origin"] == "backfill"))


def merge_duplicates(conn: sqlite3.Connection, dry_run: bool = False) -> list[tuple[str, str]]:
    """Merge stored rows that describe the same event (same rule as everywhere else).

    The row kept is the one already emailed, else the one with an LTM analysis, else the earliest stored. Links,
    sources, scale and analysis from the other row are folded in; the other row is deleted. Returns
    [(kept, removed)] descriptions."""
    rows = conn.execute("SELECT * FROM gcc ORDER BY (report_id IS NULL), (insight IS NULL), id").fetchall()
    kept: list[dict] = []
    merged: list[tuple[str, str]] = []
    for r in rows:
        d = date.fromisoformat(r["event_date"]) if r["event_date"] else None
        twin = next((k for k in kept if same_event(k["company_key"], k["city_key"], k["event_type"], k["_date"],
                                                   r["company_key"], r["city_key"], r["event_type"], d)), None)
        if twin is None:
            kept.append({**dict(r), "_date": d})
            continue
        merged.append((f"{twin['company']} ({twin['city']}, {twin['event_type']}, {twin['event_date']})",
                       f"{r['company']} ({r['city']}, {r['event_type']}, {r['event_date']}, {r['origin']})"))
        if dry_run:
            continue
        urls = list(dict.fromkeys(json.loads(twin["source_urls"] or "[]") + json.loads(r["source_urls"] or "[]")))
        srcs = list(dict.fromkeys(json.loads(twin["discovery_sources"] or "[]")
                                  + json.loads(r["discovery_sources"] or "[]")))
        fill = {k: twin[k] or r[k] for k in ("headcount", "summary", "insight", "source_url", "state")}
        if not twin["source_url"] and r["source_url"]:
            fill["source_kind"] = r["source_kind"]
        conn.execute("DELETE FROM gcc WHERE id = ?", (r["id"],))
        conn.execute(
            "UPDATE gcc SET source_urls = ?, discovery_sources = ?, headcount = ?, summary = ?, insight = ?,"
            " source_url = ?, state = ?" + (", source_kind = ?" if "source_kind" in fill else "") + " WHERE id = ?",
            (json.dumps(urls), json.dumps(srcs), fill["headcount"], fill["summary"], fill["insight"],
             fill["source_url"], fill["state"], *([fill["source_kind"]] if "source_kind" in fill else []),
             twin["id"]),
        )
        twin.update(source_urls=json.dumps(urls), discovery_sources=json.dumps(srcs), **fill)
    return merged


def pending_unreported(conn: sqlite3.Connection, since: date) -> list[GCC]:
    """GCCs the daily refresh (or a failed weekly send) stored that no email has carried yet."""
    rows = conn.execute(
        "SELECT * FROM gcc WHERE report_id IS NULL AND origin IN ('daily', 'weekly')"
        " AND (event_date >= ? OR substr(first_reported_at, 1, 10) >= ?) ORDER BY event_date",
        (since.isoformat(), since.isoformat()),
    ).fetchall()
    return [row_to_gcc(r) for r in rows]


def mark_reported(conn: sqlite3.Connection, event_keys: Iterable[str], report_id: int) -> int:
    n = 0
    for key in event_keys:
        n += conn.execute("UPDATE gcc SET report_id = ? WHERE event_key = ? AND report_id IS NULL",
                          (report_id, key)).rowcount
    return n


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
                " event_date, headcount, summary, press_release_url, source_url, source_kind, insight, source_urls,"
                " discovery_sources, event_key, first_reported_at, report_id)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    it.company, it.company_key, it.city, it.city_key, it.state, it.sector, it.event_type,
                    it.status, it.event_date.isoformat() if it.event_date else None, it.headcount, it.summary,
                    canonical_url(it.press_release_url), it.source_url, it.source_kind,
                    it.insight.model_dump_json() if it.insight else None,
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
    items = [row_to_gcc(r) for r in conn.execute("SELECT * FROM gcc WHERE report_id = ? ORDER BY id", (row["id"],))]
    return dict(row), items


def row_to_gcc(r) -> GCC:
    """A `gcc` table row as a GCC model (with its stored LTM insight, if any)."""
    return GCC(
        company=r["company"], city=r["city"], state=r["state"] or "", sector=r["sector"] or "Other",
        event_type=r["event_type"], status=r["status"],
        event_date=date.fromisoformat(r["event_date"]) if r["event_date"] else None,
        headcount=r["headcount"] or "", summary=r["summary"] or "",
        press_release_url=r["press_release_url"] or "", source_url=r["source_url"] or "",
        source_kind=r["source_kind"] or "news", source_urls=json.loads(r["source_urls"] or "[]"),
        discovery_sources=json.loads(r["discovery_sources"] or "[]"),
        insight=LTMInsight.model_validate_json(r["insight"]) if r["insight"] else None,
    )


def save_catalog(conn: sqlite3.Connection, items: Iterable[GCC], origin: str = "backfill") -> int:
    """Store items found by a backfill (no report). Already-known events are skipped. Returns rows added."""
    now = datetime.now().isoformat(timespec="seconds")
    added = 0
    for it in items:
        if is_duplicate(conn, it):
            continue
        try:
            conn.execute(
                "INSERT INTO gcc (company, company_key, city, city_key, state, sector, event_type, status,"
                " event_date, headcount, summary, press_release_url, source_url, source_kind, insight, origin,"
                " source_urls, discovery_sources, event_key, first_reported_at, report_id)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL)",
                (
                    it.company, it.company_key, it.city, it.city_key, it.state, it.sector, it.event_type,
                    it.status, it.event_date.isoformat() if it.event_date else None, it.headcount, it.summary,
                    canonical_url(it.press_release_url), it.source_url, it.source_kind,
                    it.insight.model_dump_json() if it.insight else None, origin,
                    json.dumps(it.source_urls), json.dumps(it.discovery_sources), it.event_key, now,
                ),
            )
            added += 1
        except sqlite3.IntegrityError:
            continue
    return added


def get_setting(conn: sqlite3.Connection, key: str, default: str = "") -> str:
    row = conn.execute("SELECT value FROM app_settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute("INSERT INTO app_settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = ?",
                 (key, value, value))


def search(conn: sqlite3.Connection, start: Optional[str] = None, end: Optional[str] = None,
           month: Optional[str] = None, city: Optional[str] = None, sector: Optional[str] = None,
           event_type: Optional[str] = None, fit: Optional[str] = None, q: Optional[str] = None) -> list:
    """Dashboard query. Every filter is optional; all given filters must match. Returns sqlite rows."""
    sql, args = "SELECT * FROM gcc WHERE event_date IS NOT NULL", []
    if start:
        sql += " AND event_date >= ?"
        args.append(start)
    if end:
        sql += " AND event_date <= ?"
        args.append(end)
    if month:
        sql += " AND substr(event_date, 1, 7) = ?"
        args.append(month)
    if city:
        sql += " AND city_key = ?"
        from .models import canonical_city
        args.append(canonical_city(city))
    if sector:
        sql += " AND sector = ?"
        args.append(sector)
    if event_type:
        sql += " AND event_type = ?"
        args.append(event_type)
    if fit:
        if fit == "none":
            sql += " AND insight IS NULL"
        else:
            sql += " AND json_extract(insight, '$.fit') = ?"
            args.append(fit)
    if q:
        like = f"%{q.strip().lower()}%"
        sql += (" AND (lower(company) LIKE ? OR lower(city) LIKE ? OR lower(sector) LIKE ? OR lower(summary) LIKE ?"
                " OR lower(COALESCE(insight, '')) LIKE ?)")
        args += [like] * 5
    sql += " ORDER BY event_date DESC, company"
    return conn.execute(sql, args).fetchall()


def update_insight(conn: sqlite3.Connection, gcc_id: int, insight: LTMInsight) -> None:
    conn.execute("UPDATE gcc SET insight = ? WHERE id = ?", (insight.model_dump_json(), gcc_id))


def save_insights(conn: sqlite3.Connection, items: Iterable[GCC]) -> int:
    """Store (or refresh) the LTM insight of already-saved items, matched by event_key."""
    n = 0
    for it in items:
        if it.insight is not None:
            n += conn.execute("UPDATE gcc SET insight = ? WHERE event_key = ?",
                              (it.insight.model_dump_json(), it.event_key)).rowcount
    return n
