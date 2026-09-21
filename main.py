"""CLI for the GCC India weekly report pipeline.

  python main.py --once [--dry-run] [--only serper,et_gcc]
  python main.py --schedule [--interval-min 5 | --cron "mon 09:00"]
  python main.py --list --year 2026 [--sector BFSI --city Hyderabad --type expansion]
  python main.py --stats --year 2026
  python main.py --export-csv gcc_2026.csv [--year 2026]
  python main.py --resend-last [--dry-run]
  python main.py --reset-db
"""
import argparse
import json
import logging
import sys
from pathlib import Path

from gcc_agents import config, db
from gcc_agents.graph import build_graph, run_once
from gcc_agents.tracing import init_tracing

log = logging.getLogger("gcc")


def setup_logging() -> None:
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    for handler in (logging.StreamHandler(sys.stdout), logging.FileHandler(config.DATA_DIR / "run.log", encoding="utf-8")):
        handler.setFormatter(fmt)
        root.addHandler(handler)
    for noisy in ("httpx", "httpcore", "openai", "groq", "apscheduler.scheduler", "apscheduler.executors.default"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def safe_run(settings, dry_run: bool) -> None:
    """One pipeline run; a crash is logged and never kills the scheduler."""
    try:
        run_once(settings, dry_run=dry_run)
    except Exception:
        log.exception("pipeline run crashed")


def resend_last(settings, dry_run: bool) -> int:
    """Re-render the last sent report from stored items and send it again. Does not change any stored items."""
    import base64
    from datetime import date

    from gcc_agents import email_agent, report_agent

    with db.connect() as conn:
        loaded = db.load_report(conn)
    if loaded is None:
        print("no sent report found in the database")
        return 1
    row, items = loaded
    rep = report_agent.build(settings, items, date.fromisoformat(row["window_start"]),
                             date.fromisoformat(row["window_end"]), 0, {})
    if dry_run:
        html = rep.html
        if rep.chart_png:
            html = html.replace(f"cid:{rep.chart_cid}", "data:image/png;base64," + base64.b64encode(rep.chart_png).decode())
        (config.DATA_DIR / "preview.html").write_text(html, encoding="utf-8")
        print(f"wrote data/preview.html ({len(items)} items from report {row['id']})")
        return 0
    sent, detail = email_agent.send(settings, rep)
    print(f"resend of report {row['id']} ({len(items)} items): {'sent' if sent else 'FAILED - ' + detail}")
    return 0 if sent else 2


def parse_cron(expr: str) -> dict:
    """'mon 09:00' -> CronTrigger kwargs."""
    try:
        day, hhmm = expr.split()
        hour, minute = hhmm.split(":")
        return {"day_of_week": day.lower()[:3], "hour": int(hour), "minute": int(minute)}
    except ValueError:
        raise SystemExit('--cron must look like "mon 09:00"')


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--once", action="store_true", help="run the pipeline once")
    ap.add_argument("--schedule", action="store_true", help="run on a schedule (blocking)")
    ap.add_argument("--interval-min", type=int, default=5, help="schedule interval in minutes (default 5)")
    ap.add_argument("--cron", help='weekly schedule instead of interval, e.g. "mon 09:00"')
    ap.add_argument("--dry-run", action="store_true", help="write data/preview.html; no email, no DB writes")
    ap.add_argument("--only", help="comma list of sources: " + ",".join(config.ALL_SOURCES))
    ap.add_argument("--list", action="store_true", help="list stored GCC items")
    ap.add_argument("--stats", action="store_true", help="summary counts of stored items")
    ap.add_argument("--export-csv", metavar="PATH", help="export stored items to CSV")
    ap.add_argument("--year", type=int)
    ap.add_argument("--sector")
    ap.add_argument("--city")
    ap.add_argument("--type", choices=["new_gcc", "expansion"])
    ap.add_argument("--resend-last", action="store_true",
                    help="rebuild the last sent report from the database and email it again (uses the current layout)")
    ap.add_argument("--graph", action="store_true", help="print the LangGraph structure as Mermaid and exit")
    ap.add_argument("--reset-db", action="store_true", help="delete all stored items/reports (testing)")
    args = ap.parse_args()
    setup_logging()
    settings = config.get_settings()

    if args.graph:
        print(build_graph(settings).get_graph().draw_mermaid())
        return 0
    log.info("LangSmith tracing: %s", init_tracing())

    if args.only:
        settings.only_sources = {s.strip() for s in args.only.split(",") if s.strip()}
        unknown = settings.only_sources - set(config.ALL_SOURCES)
        if unknown:
            raise SystemExit(f"unknown source(s): {', '.join(sorted(unknown))}")

    if args.reset_db:
        with db.connect() as conn:
            db.reset(conn)
        print("database cleared")
        return 0
    if args.list or args.stats or args.export_csv:
        with db.connect() as conn:
            if args.export_csv:
                n = db.export_csv(conn, Path(args.export_csv), args.year)
                print(f"exported {n} rows to {args.export_csv}")
            if args.stats:
                print(json.dumps(db.stats(conn, args.year), indent=2))
            if args.list:
                for r in db.list_items(conn, args.year, args.sector, args.city, args.type):
                    print(f"{r['event_date'] or '-':10}  {r['event_type']:9}  {r['company']} | {r['city']} | "
                          f"{r['sector']} | {r['source_kind']}: {r['source_url']}")
        return 0

    if args.resend_last:
        return resend_last(settings, dry_run=args.dry_run)

    if args.schedule:
        from apscheduler.schedulers.blocking import BlockingScheduler
        from apscheduler.triggers.cron import CronTrigger
        from apscheduler.triggers.interval import IntervalTrigger
        from datetime import datetime

        sched = BlockingScheduler()
        if args.cron:
            sched.add_job(safe_run, CronTrigger(**parse_cron(args.cron)), args=[settings, args.dry_run],
                          max_instances=1, coalesce=True)
            log.info("scheduled weekly: %s", args.cron)
        else:
            sched.add_job(safe_run, IntervalTrigger(minutes=args.interval_min), args=[settings, args.dry_run],
                          max_instances=1, coalesce=True, next_run_time=datetime.now())
            log.info("scheduled every %d minute(s); Ctrl+C to stop", args.interval_min)
        try:
            sched.start()
        except (KeyboardInterrupt, SystemExit):
            log.info("scheduler stopped")
        return 0

    if args.once or args.dry_run:
        final = run_once(settings, dry_run=args.dry_run)
        print(f"outcome: {final.get('outcome')}  approved: {len(final.get('approved', []))}")
        return 0 if final.get("outcome") != "search_failed" else 2

    ap.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


