# GCC India Weekly — multi-agent email report

LangGraph pipeline that finds **new GCCs and GCC expansions in India** from the last 7 days, attaches the best
available source link to each (press release, company post or news), never repeats an item already reported,
stores everything in SQLite, and emails a mobile-friendly report (cards + sector pie chart) to your Gmail. If nothing qualifies you get a
"no new GCC activity this week" email instead.

```
prepare → [ serper (Google) | x (Groq, x.com) | sites (ET GCC, GCC Pulse, Moneycontrol, GCC Index) ] → evaluate
        → report → email        (nothing approved → one Serper retry → empty-week report)
```

| Agent | File | Job |
|---|---|---|
| search / source | `serper_source.py`, `x_source.py`, `sites_source.py` + `sites/` | gather candidates (OpenAI extracts structured items) |
| evaluate | `evaluate_agent.py` | in scope + in window + genuine India GCC (LLM judge) + not already reported + best available source link (press release preferred, else company post, else news) |
| report | `report_agent.py`, `templates/report.html.j2` | HTML/text email, sector pie chart |
| email | `email_agent.py` | Gmail SMTP; DB is written **only after a successful send** |

## Setup

```bash
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python -m playwright install chromium
copy .env.example .env      # then fill in the keys
```

Keys you need in `.env`:

- `OPENAI_API_KEY` — all LLM steps (`OPENAI_MODEL`, `OPENAI_JUDGE_MODEL` are configurable).
- `SERPER_API_KEY` — Google search via [serper.dev](https://serper.dev) (free tier).
- `GROQ_API_KEY` — X.com lead search (Groq Compound, `include_domains=x.com`).
- `GMAIL_USER`, `GMAIL_APP_PASSWORD`, `TO_EMAIL` — create the app password at
  Google Account → Security → 2-Step Verification → App passwords.
- `GCCINDEX_USERNAME`, `GCCINDEX_PASSWORD` — your gccindex.in login (typed only into its own login form; the
  session is cached in `data/gccindex_state.json`). Leave blank to skip that source.

Any source can be disabled with `DISABLED_SOURCES=gccindex,groq_x` (names: `serper,groq_x,et_gcc,gcc_pulse,moneycontrol,gccindex`).

## Run

```bash
python main.py --once --dry-run      # writes data/preview.html, no email, no DB writes
python main.py --once                # real run: emails you and records items
python main.py --schedule            # every 5 minutes (testing)
python main.py --schedule --cron "mon 09:00"   # weekly, once you are happy
python main.py --once --dry-run --only et_gcc,gcc_pulse   # test individual sources
python main.py --resend-last [--dry-run]   # re-render and re-send the last sent report (e.g. after a layout change)
```

While testing on the 5-minute loop: after the first email, dedupe means later runs find nothing new, so you
will get a "no new GCC activity" email every 5 minutes (that is the requested behaviour). Set
`SEND_EMPTY_REPORT=false` to silence those, or use `--reset-db` to start over.

## Understand the code / graph

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for Mermaid diagrams (graph, evaluate funnel, run sequence,
database) and a module-by-module walkthrough. `python main.py --graph` prints LangGraph's own Mermaid output.

## LangSmith tracing

Set `LANGSMITH_TRACING=true` and `LANGSMITH_API_KEY` in `.env` (project defaults to `gcc-india-weekly`). Each run
appears as `gcc_weekly_run`, tagged `live` or `dry-run`, with the full node tree, every OpenAI call, and spans for
Serper, Groq, site scraping, URL checks and email. API keys are never included in spans. Traces do contain the
text sent to the LLM; set `LANGSMITH_HIDE_INPUTS=true` / `LANGSMITH_HIDE_OUTPUTS=true` to avoid storing it.

## Query the database (whole year, any time)

```bash
python main.py --list --year 2026
python main.py --list --sector BFSI --city Hyderabad --type expansion
python main.py --stats --year 2026
python main.py --export-csv gcc_2026.csv --year 2026
```

`data/gcc.db` tables: `gcc` (one row per reported event) and `reports` (one row per run, including empty weeks
and failed sends). Sent report HTML is archived in `data/reports/`.

## Rules worth knowing

- **Scope:** `new_gcc` or `expansion`; `opened` or (`INCLUDE_ANNOUNCED=true`) officially `announced`. Rumours,
  hiring-only news, appointments, layoffs are rejected.
- **Dedupe:** a `(company, city)` is "new" only once ever; the same expansion story isn't repeated but a later
  expansion (different month) can appear; the press-release URL is also unique.
- **Source links:** each row shows what kind of link it is. A verified official press release / newsroom page
  is preferred; if none exists the company's own LinkedIn/X post is used, then a news article. Items are not
  rejected for lacking an official press release. They are still rejected if undated, outside the window, not a
  genuine India GCC event, already reported, or if no source link exists at all.
- **Never a false "nothing new":** if every source errors out, no email is sent and the run is logged as
  `search_failed`. Per-source status is in `data/run.log`.
- **Moneycontrol** blocks headless browsers (Access Denied). We don't evade that; its coverage comes from the
  Serper `site:moneycontrol.com` query.
- **GCC Index:** the adapter logs in with your own account, lets the events page load its own JSON feed and parses
  it (exact dates, type, city, sector). It never calls the feed directly, which the site refuses. The raw capture is
  written to `data/gccindex_debug.txt`. Check the site's terms before scheduling it.

## Tests

```bash
python -m pytest -q
```
