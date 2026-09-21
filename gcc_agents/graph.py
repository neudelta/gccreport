"""LangGraph wiring:

  prepare -> [serper | x | sites] (parallel) -> evaluate -> report -> email
                                                  |  ^
                                   (nothing approved, one retry) serper_retry
"""
import base64
import logging
import operator
from datetime import date, datetime, timedelta
from typing import Annotated, Optional, TypedDict

from langgraph.graph import END, START, StateGraph

from . import config, db, email_agent, evaluate_agent, report_agent, serper_source, sites_source, x_source
from .config import Settings
from .models import GCC
from .report_agent import Report

log = logging.getLogger("gcc")
MAX_RETRIES = 1


def _merge(a: dict, b: dict) -> dict:
    return {**(a or {}), **(b or {})}


class State(TypedDict, total=False):
    today: date
    window_start: date
    window_end: date
    reported: list[str]
    candidates: Annotated[list[GCC], operator.add]
    approved: Annotated[list[GCC], operator.add]
    rejected: Annotated[list[dict], operator.add]
    already_reported_count: Annotated[int, operator.add]
    source_status: Annotated[dict, _merge]
    processed: int
    attempts: int
    report: Optional[Report]
    outcome: str


def _any_source_ok(status: dict) -> bool:
    return any(v.startswith("ok") for v in status.values())


def build_graph(settings: Settings, dry_run: bool = False, today: Optional[date] = None):
    def prepare(state: State) -> dict:
        t = today or date.today()
        with db.connect() as conn:
            reported = db.reported_summary(conn)
        return {
            "today": t, "window_start": t - timedelta(days=settings.lookback_days), "window_end": t,
            "reported": reported, "processed": 0, "attempts": 0, "outcome": "",
        }

    def serper_node(state: State) -> dict:
        if not settings.source_enabled("serper"):
            return {"source_status": {"serper": "skipped: disabled"}}
        items, status = serper_source.run(settings, state["today"], state["reported"], attempt=0)
        return {"candidates": items, "source_status": {"serper": status}}

    def x_node(state: State) -> dict:
        if not settings.source_enabled("groq_x"):
            return {"source_status": {"groq_x": "skipped: disabled"}}
        items, status = x_source.run(settings, state["today"], state["reported"])
        return {"candidates": items, "source_status": {"groq_x": status}}

    def sites_node(state: State) -> dict:
        items, status = sites_source.run(settings, state["today"], state["reported"])
        return {"candidates": items, "source_status": status}

    def serper_retry(state: State) -> dict:
        items, status = serper_source.run(settings, state["today"], state["reported"], attempt=1)
        return {"candidates": items, "attempts": state["attempts"] + 1,
                "source_status": {"serper": f"{state['source_status'].get('serper', '')} | retry {status}"}}

    def evaluate(state: State) -> dict:
        new = state["candidates"][state["processed"]:] if state.get("candidates") else []
        with db.connect() as conn:
            approved, rejected, dups = evaluate_agent.run(settings, conn, new, state["today"])
        log.info("evaluate: %d candidates -> %d approved, %d rejected, %d already reported",
                 len(new), len(approved), len(rejected), dups)
        outside = [r for r in rejected if r["reason"].startswith("event date")]
        for r in rejected:
            if r not in outside:
                log.info("  rejected %s (%s): %s", r["company"], r["city"], r["reason"])
        if outside:  # GCC Index alone returns ~100 older rows, so summarise instead of one line each
            log.info("  %d candidate(s) rejected as outside the %d-day window", len(outside), settings.lookback_days)
        return {"approved": approved, "rejected": rejected, "already_reported_count": dups,
                "processed": len(state.get("candidates", []))}

    def route_after_evaluate(state: State) -> str:
        if state.get("approved"):
            return "report"
        if not _any_source_ok(state["source_status"]):
            return "search_failed"
        if state["attempts"] < MAX_RETRIES and settings.source_enabled("serper") and settings.serper_key:
            return "serper_retry"
        return "report"

    def search_failed(state: State) -> dict:
        log.error("all sources failed; not claiming 'no new GCCs': %s", state["source_status"])
        if not dry_run:
            with db.connect() as conn:
                db.save_report(conn, window_start=state["window_start"], window_end=state["window_end"],
                               items=[], email_status="search_failed", sent_at=None, html_path=None,
                               source_status=state["source_status"])
        return {"outcome": "search_failed"}

    def report(state: State) -> dict:
        rep = report_agent.build(settings, state.get("approved", []), state["window_start"],
                                 state["window_end"], state.get("already_reported_count", 0),
                                 state["source_status"])
        return {"report": rep}

    def route_after_report(state: State) -> str:
        if not state.get("approved") and not settings.send_empty_report:
            return "skip"
        return "email"

    def skip(state: State) -> dict:
        return {"outcome": "nothing_to_send"}

    def email(state: State) -> dict:
        rep = state["report"]
        if dry_run:
            config.DATA_DIR.mkdir(parents=True, exist_ok=True)
            html = rep.html
            if rep.chart_png:  # browsers can't resolve cid: URLs, so inline the chart for the preview only
                html = html.replace(f"cid:{rep.chart_cid}",
                                    "data:image/png;base64," + base64.b64encode(rep.chart_png).decode())
            (config.DATA_DIR / "preview.html").write_text(html, encoding="utf-8")
            log.info("dry run: wrote data/preview.html (%d items)", len(state.get("approved", [])))
            return {"outcome": "dry_run"}
        sent, detail = email_agent.send(settings, rep)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        html_path = None
        if sent:
            config.REPORTS_DIR.mkdir(parents=True, exist_ok=True)
            html_path = str(config.REPORTS_DIR / f"report_{stamp}.html")
            (config.REPORTS_DIR / f"report_{stamp}.html").write_text(rep.html, encoding="utf-8")
        with db.connect() as conn:
            # Items are committed only after a successful send, so a failed send never "burns" them.
            _, saved = db.save_report(
                conn, window_start=state["window_start"], window_end=state["window_end"],
                items=state.get("approved", []) if sent else [], email_status="sent" if sent else "send_failed",
                sent_at=datetime.now() if sent else None, html_path=html_path,
                source_status=state["source_status"],
            )
        if sent:
            log.info("email sent (%d GCC items saved to DB)", saved)
            return {"outcome": "sent"}
        log.error("email failed: %s", detail)
        return {"outcome": "send_failed"}

    g = StateGraph(State)
    for name, fn in [("prepare", prepare), ("serper", serper_node), ("x", x_node), ("sites", sites_node),
                     ("serper_retry", serper_retry), ("evaluate", evaluate), ("search_failed", search_failed),
                     ("report", report), ("skip", skip), ("email", email)]:
        g.add_node(name, fn)
    g.add_edge(START, "prepare")
    for src in ("serper", "x", "sites"):
        g.add_edge("prepare", src)
    g.add_edge(["serper", "x", "sites"], "evaluate")
    g.add_edge("serper_retry", "evaluate")
    g.add_conditional_edges("evaluate", route_after_evaluate,
                            {"report": "report", "search_failed": "search_failed", "serper_retry": "serper_retry"})
    g.add_conditional_edges("report", route_after_report, {"email": "email", "skip": "skip"})
    for terminal in ("search_failed", "skip", "email"):
        g.add_edge(terminal, END)
    return g.compile()


def run_once(settings: Settings, dry_run: bool = False, today: Optional[date] = None) -> State:
    graph = build_graph(settings, dry_run=dry_run, today=today)
    # These show up in LangSmith to make runs easy to filter (tags) and inspect (metadata).
    trace_cfg = {
        "run_name": "gcc_weekly_run",
        "tags": ["gcc-india", "dry-run" if dry_run else "live"],
        "metadata": {
            "dry_run": dry_run,
            "lookback_days": settings.lookback_days,
            "include_announced": settings.include_announced,
            "sources": [s for s in config.ALL_SOURCES if settings.source_enabled(s)],
            "openai_model": settings.openai_model,
            "judge_model": settings.judge_model,
        },
    }
    final = graph.invoke({}, config=trace_cfg)
    log.info("run finished: outcome=%s approved=%d sources=%s", final.get("outcome"),
             len(final.get("approved", [])), final.get("source_status"))
    return final
