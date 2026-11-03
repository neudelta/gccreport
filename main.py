"""CLI for the GCC India weekly report pipeline.

  python main.py --once [--dry-run] [--only serper,et_gcc]
  python main.py --schedule [--interval-min 5 | --cron "mon 09:00"]
  python main.py --list --year 2026 [--sector BFSI --city Hyderabad --type expansion]
  python main.py --stats --year 2026
  python main.py --export-csv gcc_2026.csv [--year 2026]
  python main.py --resend-last [--dry-run]
  python main.py --backfill 2026 [--dry-run] [--no-google]
  python main.py --refresh                 (daily: add new GCCs to the database, no email)
  python main.py --schedule --daily 07:00 --cron "mon 09:00"
  python main.py --dashboard [--port 8000]
  python main.py --dedupe-db [--dry-run]
  python main.py --why "TIAA, Havells"     (what happened to these companies in the last run)
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


def safe_run(settings, dry_run: bool, mode: str = "email") -> None:
    """One pipeline run; a crash is logged and never kills the scheduler."""
    try:
        run_once(settings, dry_run=dry_run, mode=mode)
    except Exception:
        log.exception("pipeline run (%s) crashed", mode)


def port_problem(port: int) -> str:
    """Empty if the port is free; otherwise a plain explanation of what to do."""
    import socket
    import urllib.request

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        if sock.connect_ex(("127.0.0.1", port)) != 0:
            return ""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/facets", timeout=3) as resp:
            if resp.status == 200:
                return (f"The dashboard is already running: open http://127.0.0.1:{port}\n"
                        "(close the other window that runs it first if you want to restart it).")
    except Exception:
        pass
    return (f"Port {port} is used by another program. Start the dashboard on another port, e.g.\n"
            f"  python main.py --dashboard --port {port + 80}")


def explain(names: str) -> int:
    """For each name: found by which source, and approved / rejected (why), or not found at all."""
    from gcc_agents.models import normalize_company

    path = config.DATA_DIR / "last_run.json"
    if not path.exists():
        print("no run recorded yet - run `python main.py --once --dry-run` first")
        return 1
    data = json.loads(path.read_text(encoding="utf-8"))
    print(f"last run window: {data.get('window')}")
    for name in [n.strip() for n in names.split(",") if n.strip()]:
        flat = lambda s: "".join(normalize_company(s).split())  # noqa: E731
        key = flat(name) or name.lower()
        hits = [c for c in data["candidates"] if key in flat(c["company"])]
        decisions = [d for d in data["decisions"] if key in flat(d["company"])]
        if not hits:
            print(f"- {name}: NOT FOUND by any source (coverage gap: add a source or search phrase)")
            continue
        srcs = sorted({s for c in hits for s in c["sources"]})
        print(f"- {name}: found {len(hits)}x via {', '.join(srcs)}")
        for d in decisions:
            print(f"    {d['outcome']}: {d['company']} ({d['city']})" + (f" - {d['reason']}" if d.get("reason") else ""))
        if not decisions:
            print("    merged into another item or counted as already reported")
    return 0


def resend_last(settings, dry_run: bool) -> int:
    """Re-render the last sent report from stored items (current layout) and send it again.

    Items that were stored before the LTM insights existed are profiled now, and their insight is saved, so the
    detailed briefing is complete. No other stored data changes.
    """
    from datetime import date

    from gcc_agents import email_agent, insights_agent, report_agent
    from gcc_agents.graph import write_previews

    with db.connect() as conn:
        loaded = db.load_report(conn)
    if loaded is None:
        print("no sent report found in the database")
        return 1
    row, items = loaded
    missing = [i for i in items if i.insight is None]
    if missing:
        items = insights_agent.enrich(settings, items)
        with db.connect() as conn:
            db.save_insights(conn, items)
    rep = report_agent.build(settings, items, date.fromisoformat(row["window_start"]),
                             date.fromisoformat(row["window_end"]), 0, {})
    if dry_run:
        write_previews(rep)
        print(f"wrote data/preview.html{' and data/preview_briefing.pdf' if rep.pdf else ''} "
              f"({len(items)} items from report {row['id']})")
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
    ap.add_argument("--cron", help='weekly email schedule instead of interval, e.g. "mon 09:00"')
    ap.add_argument("--daily", metavar="HH:MM", help='with --schedule: daily refresh time, e.g. "07:00"')
    ap.add_argument("--refresh", action="store_true",
                    help="daily refresh: scan the last few days and add new GCCs to the database (no email)")
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
    ap.add_argument("--backfill", type=int, metavar="YEAR",
                    help="load that year's GCCs (GCC Index + month-by-month Google) into the database")
    ap.add_argument("--dedupe-db", action="store_true",
                    help="merge stored rows that describe the same GCC event (add --dry-run to only list them)")
    ap.add_argument("--fix-sectors", action="store_true", help="re-classify stored rows whose sector is 'Other'")
    ap.add_argument("--no-google", action="store_true", help="with --backfill: GCC Index only")
    ap.add_argument("--dashboard", action="store_true", help="run the LTM dashboard at http://127.0.0.1:8000")
    ap.add_argument("--port", type=int, default=8000, help="dashboard port (default 8000)")
    ap.add_argument("--why", metavar="NAMES",
                    help='explain what the last run did with these companies, e.g. --why "TIAA, Havells"')
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

    if args.why:
        return explain(args.why)

    if args.dedupe_db:
        with db.connect() as conn:
            merged = db.merge_duplicates(conn, dry_run=args.dry_run)
        for kept, removed in merged:
            print(f"  keep {kept}\n    {'would merge' if args.dry_run else 'merged'} {removed}")
        print(f"{len(merged)} duplicate(s) {'found (dry run, nothing changed)' if args.dry_run else 'merged'}")
        return 0

    if args.fix_sectors:
        from gcc_agents import backfill
        print(f"re-classified {backfill.reclassify_sectors(settings)} rows")
        return 0

    if args.backfill:
        from gcc_agents import backfill
        res = backfill.run(settings, args.backfill, dry_run=args.dry_run, use_google=not args.no_google)
        print(f"backfill {args.backfill} ({res.start} to {res.end}): GCC Index rows {res.gccindex_rows}, "
              f"Google candidates {res.google_candidates}, kept {len(res.kept)}, rejected {len(res.rejected)}, "
              + ("dry run: see data/backfill_preview.csv" if args.dry_run else f"added {res.added} to the database"))
        return 0

    if args.dashboard:
        busy = port_problem(args.port)
        if busy:
            print(busy)
            return 1
        import uvicorn
        print(f"LTM GCC dashboard: http://127.0.0.1:{args.port}  (Ctrl+C to stop)")
        uvicorn.run("dashboard.app:app", host="127.0.0.1", port=args.port, log_level="warning")
        return 0

    if args.resend_last:
        return resend_last(settings, dry_run=args.dry_run)

    if args.schedule:
        from apscheduler.schedulers.blocking import BlockingScheduler
        from apscheduler.triggers.cron import CronTrigger
        from apscheduler.triggers.interval import IntervalTrigger
        from datetime import datetime

        sched = BlockingScheduler()
        if args.daily:
            hh, mm = (int(x) for x in args.daily.split(":"))
            sched.add_job(safe_run, CronTrigger(hour=hh, minute=mm), args=[settings, args.dry_run, "collect"],
                          max_instances=1, coalesce=True, misfire_grace_time=3600)
            log.info("scheduled daily refresh: %s", args.daily)
        if args.cron:
            sched.add_job(safe_run, CronTrigger(**parse_cron(args.cron)), args=[settings, args.dry_run],
                          max_instances=1, coalesce=True)
            log.info("scheduled weekly: %s", args.cron)
        elif not args.daily:
            sched.add_job(safe_run, IntervalTrigger(minutes=args.interval_min), args=[settings, args.dry_run],
                          max_instances=1, coalesce=True, next_run_time=datetime.now())
            log.info("scheduled every %d minute(s); Ctrl+C to stop", args.interval_min)
        try:
            sched.start()
        except (KeyboardInterrupt, SystemExit):
            log.info("scheduler stopped")
        return 0

    if args.refresh:
        final = run_once(settings, dry_run=args.dry_run, mode="collect")
        print(f"outcome: {final.get('outcome')}  new GCCs: {len(final.get('approved', []))}")
        return 0 if final.get("outcome") != "search_failed" else 2

    if args.once or args.dry_run:
        final = run_once(settings, dry_run=args.dry_run)
        print(f"outcome: {final.get('outcome')}  approved: {len(final.get('approved', []))}")
        return 0 if final.get("outcome") != "search_failed" else 2

    ap.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


