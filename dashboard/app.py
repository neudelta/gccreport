"""LTM GCC dashboard: a local web app over data/gcc.db.

Run with `python main.py --dashboard` (binds to 127.0.0.1 only). Endpoints:
  GET  /                       the dashboard page (static/index.html)
  GET  /api/facets             filter options (months, cities, sectors) and the data range
  GET  /api/gccs?filters       matching GCCs + KPIs + chart series
  GET  /api/gcc/{id}           one GCC with its LTM analysis (if any)
  POST /api/gcc/{id}/analyse   run the LTM analysis for one GCC and save it
  GET  /api/report.pdf?filters download the LTM briefing for the current filter
  GET  /api/export.csv?filters download the filtered rows as CSV
  GET  /api/recipients         saved default recipients
  POST /api/email              email the brief + PDF for the current filter to the given recipients

Filters (all optional): start, end (YYYY-MM-DD), month (YYYY-MM), city, sector, type (new_gcc|expansion),
fit (High|Medium|Low|none), q (free text).

Safety: state-changing calls must carry the header `X-GCC-Dashboard: 1`. Browsers cannot add that header to a
cross-site request without a CORS preflight (which this app never allows), so another website cannot make your
dashboard send email or spend API credits.
"""
import csv
import io
import logging
from collections import Counter
from datetime import date
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import FileResponse, Response, StreamingResponse
from pydantic import BaseModel

from gcc_agents import config, db, email_agent, insights_agent, report_agent, visuals

log = logging.getLogger("gcc")
STATIC = Path(__file__).resolve().parent / "static"
ANALYSE_LIMIT = 25  # reports analyse at most this many un-analysed GCCs on the fly (cost/time guard)
RECIPIENTS_KEY = "dashboard_recipients"

app = FastAPI(title="LTM GCC Radar", docs_url=None, redoc_url=None)
_hero_png: Optional[bytes] = None


# ---------------------------------------------------------------- helpers
class Filters(BaseModel):
    start: Optional[str] = None
    end: Optional[str] = None
    month: Optional[str] = None
    city: Optional[str] = None
    sector: Optional[str] = None
    type: Optional[str] = None
    fit: Optional[str] = None
    q: Optional[str] = None

    def clean(self) -> "Filters":
        data = {k: (v.strip() or None) if isinstance(v, str) else v for k, v in self.model_dump().items()}
        for key in ("start", "end"):
            if data[key]:
                try:
                    date.fromisoformat(data[key])
                except ValueError:
                    raise HTTPException(400, f"{key} must be YYYY-MM-DD")
        if data["type"] and data["type"] not in ("new_gcc", "expansion"):
            raise HTTPException(400, "type must be new_gcc or expansion")
        if data["fit"] and data["fit"] not in ("High", "Medium", "Low", "none"):
            raise HTTPException(400, "fit must be High, Medium, Low or none")
        return Filters(**data)


def filters_from_query(
    start: Optional[str] = None, end: Optional[str] = None, month: Optional[str] = None,
    city: Optional[str] = None, sector: Optional[str] = None, type: Optional[str] = Query(None),
    fit: Optional[str] = None, q: Optional[str] = None,
) -> Filters:
    return Filters(start=start, end=end, month=month, city=city, sector=sector, type=type, fit=fit, q=q).clean()


def require_dashboard_header(x_gcc_dashboard: Optional[str] = Header(None)) -> None:
    if x_gcc_dashboard != "1":
        raise HTTPException(403, "missing X-GCC-Dashboard header")


def _rows(f: Filters) -> list:
    with db.connect() as conn:
        return db.search(conn, start=f.start, end=f.end, month=f.month, city=f.city, sector=f.sector,
                         event_type=f.type, fit=f.fit, q=f.q)


def _to_json(r) -> dict:
    g = db.row_to_gcc(r)
    return {
        "id": r["id"], "company": g.company, "city": g.city, "state": g.state, "sector": g.sector,
        "type": g.event_type, "status": g.status, "date": g.event_date.isoformat() if g.event_date else None,
        "headcount": g.headcount, "summary": g.summary, "source_url": g.source_url,
        "source_label": g.source_label if g.source_url else "GCC Index",
        "source_host": g.source_host, "origin": r["origin"], "found_by": g.discovery_sources,
        "fit": g.insight.fit if g.insight else None,
        "insight": g.insight.model_dump() if g.insight else None,
    }


def _cities(rows) -> list[str]:
    """One filter entry per canonical city (Noida and Gautam Buddh Nagar are one), most GCCs first."""
    from gcc_agents.models import canonical_city
    counts: Counter = Counter()
    for r in rows:
        if r["city"]:
            counts[canonical_city(r["city"])] += 1
    return [c.title() for c, _ in counts.most_common()]


def _period(f: Filters, rows: list) -> tuple[date, date]:
    """Report period: the filter's own range if given, else the span of the rows."""
    if f.month:
        y, m = map(int, f.month.split("-"))
        import calendar
        return date(y, m, 1), date(y, m, calendar.monthrange(y, m)[1])
    dates = [date.fromisoformat(r["event_date"]) for r in rows if r["event_date"]]
    start = date.fromisoformat(f.start) if f.start else (min(dates) if dates else date.today())
    end = date.fromisoformat(f.end) if f.end else (max(dates) if dates else date.today())
    return start, end


def _build_report(f: Filters):
    rows = _rows(f)
    if not rows:
        raise HTTPException(404, "no GCCs match these filters")
    items = [db.row_to_gcc(r) for r in rows]
    settings = config.get_settings()
    # Analyse the most relevant un-analysed GCCs (new GCCs first, newest first), capped for cost and time.
    todo = sorted([i for i in items if i.insight is None],
                  key=lambda i: (i.event_type != "new_gcc", -(i.event_date.toordinal() if i.event_date else 0)))
    todo = todo[:ANALYSE_LIMIT]
    if todo:
        done = insights_agent.enrich(settings, todo)
        with db.connect() as conn:
            db.save_insights(conn, done)
        by_key = {i.event_key: i for i in done}
        items = [by_key.get(i.event_key, i) for i in items]
    start, end = _period(f, rows)
    return settings, report_agent.build(settings, items, start, end, 0, {})


# ---------------------------------------------------------------- pages
@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


@app.get("/hero.png")
def hero() -> Response:
    global _hero_png
    if _hero_png is None:
        _hero_png = visuals.hero_wave(width_in=16, height_in=2.2, seed=5)
    return Response(_hero_png, media_type="image/png", headers={"Cache-Control": "max-age=86400"})


# ---------------------------------------------------------------- data
@app.get("/api/facets")
def facets() -> dict:
    with db.connect() as conn:
        rows = conn.execute("SELECT event_date, city, sector FROM gcc WHERE event_date IS NOT NULL").fetchall()
        recipients = db.get_setting(conn, RECIPIENTS_KEY, config.get_settings().to_email)
    months = sorted({r["event_date"][:7] for r in rows}, reverse=True)
    dates = sorted(r["event_date"] for r in rows)
    return {
        "months": months,
        "cities": _cities(rows),
        "sectors": sorted({r["sector"] for r in rows if r["sector"]}),
        "min_date": dates[0] if dates else None, "max_date": dates[-1] if dates else None,
        "total": len(rows), "has_recipients": bool(recipients),
    }


@app.get("/api/gccs")
def gccs(f: Filters = Depends(filters_from_query)) -> dict:
    items = [_to_json(r) for r in _rows(f)]
    by_month = Counter(i["date"][:7] for i in items if i["date"])
    return {
        "items": items,
        "kpis": {
            "total": len(items),
            "new": sum(i["type"] == "new_gcc" for i in items),
            "expansion": sum(i["type"] == "expansion" for i in items),
            "cities": len({i["city"].split(",")[0].strip().lower() for i in items}),
            "high": sum(i["fit"] == "High" for i in items),
            "analysed": sum(i["fit"] is not None for i in items),
        },
        "charts": {
            "by_month": sorted(by_month.items()),
            "by_sector": Counter(i["sector"] for i in items).most_common(),
            "by_city": Counter(i["city"].split(",")[0].strip() for i in items).most_common(8),
        },
    }


@app.get("/api/gcc/{gcc_id}")
def gcc_detail(gcc_id: int) -> dict:
    with db.connect() as conn:
        r = conn.execute("SELECT * FROM gcc WHERE id = ?", (gcc_id,)).fetchone()
    if r is None:
        raise HTTPException(404, "not found")
    return _to_json(r)


@app.post("/api/gcc/{gcc_id}/analyse", dependencies=[Depends(require_dashboard_header)])
def analyse(gcc_id: int) -> dict:
    with db.connect() as conn:
        r = conn.execute("SELECT * FROM gcc WHERE id = ?", (gcc_id,)).fetchone()
    if r is None:
        raise HTTPException(404, "not found")
    try:
        insight = insights_agent.analyse(config.get_settings(), db.row_to_gcc(r))
    except Exception as exc:
        log.warning("analyse failed for %s: %s", r["company"], exc)
        raise HTTPException(502, f"analysis failed: {type(exc).__name__}")
    with db.connect() as conn:
        db.update_insight(conn, gcc_id, insight)
        r = conn.execute("SELECT * FROM gcc WHERE id = ?", (gcc_id,)).fetchone()
    return _to_json(r)


# ---------------------------------------------------------------- downloads
@app.get("/api/report.pdf")
def report_pdf(f: Filters = Depends(filters_from_query)) -> Response:
    _, rep = _build_report(f)
    if not rep.pdf:
        raise HTTPException(500, "the PDF could not be rendered")
    return Response(rep.pdf, media_type="application/pdf",
                    headers={"Content-Disposition": f'attachment; filename="{rep.pdf_name}"'})


@app.get("/api/export.csv")
def export_csv(f: Filters = Depends(filters_from_query)) -> StreamingResponse:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["date", "company", "type", "status", "city", "state", "sector", "headcount", "ltm_fit",
                "engagement_model", "summary", "source", "found_by"])
    for r in _rows(f):
        i = _to_json(r)
        ins = i["insight"] or {}
        w.writerow([i["date"], i["company"], i["type"], i["status"], i["city"], i["state"], i["sector"],
                    i["headcount"], i["fit"] or "", ins.get("engagement_model", ""), i["summary"],
                    i["source_url"], "+".join(i["found_by"])])
    data = "﻿" + buf.getvalue()  # BOM so Excel opens UTF-8 correctly
    return StreamingResponse(iter([data]), media_type="text/csv",
                             headers={"Content-Disposition": 'attachment; filename="LTM_GCC_list.csv"'})


# ---------------------------------------------------------------- email
class EmailRequest(BaseModel):
    filters: Filters = Filters()
    recipients: str
    save_default: bool = False


@app.get("/api/recipients")
def get_recipients() -> dict:
    with db.connect() as conn:
        return {"recipients": db.get_setting(conn, RECIPIENTS_KEY, config.get_settings().to_email)}


@app.post("/api/email", dependencies=[Depends(require_dashboard_header)])
def send_email(req: EmailRequest) -> dict:
    try:
        recipients = email_agent.parse_recipients(req.recipients)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    if not recipients:
        raise HTTPException(400, "add at least one recipient")
    if len(recipients) > 25:
        raise HTTPException(400, "at most 25 recipients")
    settings, rep = _build_report(req.filters.clean())
    sent, detail = email_agent.send(settings, rep, recipients=", ".join(recipients))
    if not sent:
        raise HTTPException(502, f"email not sent: {detail}")
    if req.save_default:
        with db.connect() as conn:
            db.set_setting(conn, RECIPIENTS_KEY, ", ".join(recipients))
    return {"sent": True, "recipients": recipients, "subject": rep.subject}
