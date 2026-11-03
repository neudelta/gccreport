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

New to the code? Start with the plain-English [docs/CODE_WALKTHROUGH.md](docs/CODE_WALKTHROUGH.md) (also as a
styled [HTML page](docs/CODE_WALKTHROUGH.html) to open locally). See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for Mermaid diagrams (graph, evaluate funnel, run sequence,
database) and a module-by-module walkthrough. `python main.py --graph` prints LangGraph's own Mermaid output.

## LangSmith tracing

Set `LANGSMITH_TRACING=true` and `LANGSMITH_API_KEY` in `.env` (project defaults to `gcc-india-weekly`). Each run
appears as `gcc_weekly_run`, tagged `live` or `dry-run`, with the full node tree, every OpenAI call, and spans for
Serper, Groq, site scraping, URL checks and email. API keys are never included in spans. Traces do contain the
text sent to the LLM; set `LANGSMITH_HIDE_INPUTS=true` / `LANGSMITH_HIDE_OUTPUTS=true` to avoid storing it.

## Daily refresh and scheduling on another machine

Two jobs keep everything current:

| Job | Command | What it does |
|---|---|---|
| Daily refresh (07:00 IST) | `python main.py --refresh` | Scans the last 3 days (`DAILY_LOOKBACK_DAYS`), same checks as the weekly run, adds new GCCs to `data/gcc.db`. They appear in the dashboard at once. No email. |
| Weekly email (e.g. Mon 09:00) | `python main.py --once` | Searches the last 2 weeks and emails everything new **plus everything the daily refresh collected since the last email**, then marks them as sent. A GCC is never emailed twice. |

**Moving to the host machine**
1. `git clone https://github.com/neudelta/gccreport.git`, then the Setup steps above (venv, requirements,
   `playwright install chromium`).
2. Copy from this PC (never committed): `.env` and the `data/` folder - `gcc.db` holds the 2026 catalogue and
   the email history; `gccindex_state.json` is the GCC Index login session (it logs in again if missing).
3. Schedule:
   - Windows: `powershell -ExecutionPolicy Bypass -File scripts\schedule_windows.ps1` (options `-DailyTime 07:00
     -WeeklyDay Monday -WeeklyTime 09:00 -Dashboard`). Uses the machine's local time; missed runs start when
     the machine is back on.
   - Linux: see `scripts/crontab.example` (`CRON_TZ=Asia/Kolkata`).
   - Or one long-running process: `python main.py --schedule --daily 07:00 --cron "mon 09:00"`.
4. Only one machine should run the jobs against a given `gcc.db`.

## LTM dashboard (live, interactive)

```bash
python main.py --backfill 2026      # one-off: load all 2026 GCCs into data/gcc.db (~12 min)
python main.py --dashboard          # then open http://127.0.0.1:8000
```

- **Filters:** date range, month, city, sector, type (new GCC / expansion), LTM fit, plus free-text search over
  company, city, sector, summary and the LTM analysis. Quick ranges: last 2 weeks, last 30 days, this quarter,
  year to date. Click a bar or slice in the charts to filter by it. The address bar keeps the filters, so a view
  can be bookmarked.
- **Detail drawer:** click a row for the summary, source and found-by. "Analyse for LTM" generates business area,
  products, the India centre's mandate and the recommended GCC Origin play, and saves it.
- **Download:** the LTM PDF briefing or a CSV (opens in Excel) for exactly what is filtered.
- **Email report:** sends the executive email + PDF for the current filter. Recipients are editable (several,
  comma-separated) and can be saved as the default. Reports analyse up to 25 un-analysed GCCs first.
- Runs on this PC only (bound to 127.0.0.1). Actions that send email or spend API credits need a header that
  other websites cannot add, so a web page you visit cannot trigger them.

**Backfill details.** Sources are your logged-in GCC Index events feed (exact dates) and Google searched one month
at a time (only results dated inside that month). Same rules as the weekly run, plus: one "new GCC" per company
and city per year (GCC Index's date preferred), and sectors left as "Other" are re-classified
(`python main.py --fix-sectors` runs that alone). Backfill stops at the end of the last emailed week, so newer
GCCs still go out in the next weekly email. Rows are tagged `origin=backfill`.

## Free demo on Hugging Face

A private Hugging Face Space (free CPU hardware) runs the dashboard from a Docker image with a **snapshot** of the
database, behind a password.

1. Create a free account at huggingface.co and a **write** token (Settings → Access Tokens).
2. Build and upload (you run this; the token stays on your PC):
   ```bash
   .venv\Scripts\python -m pip install huggingface_hub
   set HF_TOKEN=hf_your_write_token
   .venv\Scripts\python scripts\build_hf_space.py --space YOUR_HF_NAME/ltm-gcc-radar
   ```
   This creates a **private** Space and uploads the app code, `Dockerfile` and a cleaned demo copy of
   `data/gcc.db` (no saved recipients or local paths; GCCs found only via GCC Index are left out unless you add
   `--include-gccindex-only` after checking GCC Index's terms). `.env` and the GCC Index session are never uploaded.
   Without `--space` it only builds `dist/hf-space` so you can inspect it.
3. In the Space: **Settings → Variables and secrets**
   - secret `DASHBOARD_PASSWORD` — **required**; the sign-in password you share
   - variable `DEMO_MODE` = `true`, `DEMO_DAILY_ANALYSES` = e.g. `30`, `DEMO_EMAIL_DOMAINS` = e.g. `ltm.com`
   - secret `OPENAI_API_KEY` (+ `SERPER_API_KEY`) to allow "Analyse for LTM"
   - secrets `GMAIL_USER`, `GMAIL_APP_PASSWORD` to allow emailing reports (optional)
4. The Space builds (about 5 minutes) and shows the sign-in page. To share it, add people to the Space
   (private Spaces need a Hugging Face account) or make it public (the password still protects it).

**Demo behaviour:** sign-in for everything; a "Demo" badge; emails only to the allowed domains; a daily cap on
new AI analyses so a shared link cannot run up your OpenAI bill. The demo does not run the daily refresh; to
update it, rebuild and upload again. Free Spaces sleep after a period without visitors and restart with the
uploaded snapshot (analyses made in the demo are not kept).

Test the same image locally (needs Docker):
```bash
cd dist\hf-space
docker build -t ltm-gcc-radar-demo .
docker run --rm -p 7860:7860 -e DASHBOARD_PASSWORD=choose-one -e DEMO_MODE=true ltm-gcc-radar-demo
```
then open http://127.0.0.1:7860.

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
