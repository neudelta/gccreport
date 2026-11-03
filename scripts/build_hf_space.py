"""Assemble (and optionally upload) the Hugging Face Space for the dashboard demo.

    python scripts/build_hf_space.py                         # builds dist/hf-space (nothing is uploaded)
    python scripts/build_hf_space.py --space YOUR_NAME/ltm-gcc-radar   # builds and uploads (private Space)

The folder contains the app code, the Dockerfile, a Space README and a *cleaned demo copy* of data/gcc.db
(no saved settings or local paths; GCC Index-only rows left out unless --include-gccindex-only). It never
contains .env, your GCC Index session or other local data.

Uploading needs `pip install huggingface_hub` and a Hugging Face write token, given by you: either
`huggingface-cli login` beforehand or the HF_TOKEN environment variable. Keys for the running Space (OpenAI,
dashboard password, Gmail) are added by you in the Space's Settings -> Variables and secrets, never uploaded.
"""
import argparse
import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

CODE = ["main.py", "requirements.txt", "Dockerfile", "gcc_agents", "dashboard"]

SPACE_README = """---
title: LTM GCC Radar
emoji: 📊
colorFrom: red
colorTo: gray
sdk: docker
app_port: 7860
pinned: false
short_description: India GCC intelligence dashboard (demo)
---

# India GCC Intelligence Dashboard — demo

LTM GCC Origin's market radar: new Global Capability Centres and expansions in India, with filters, search,
LTM opportunity analysis, PDF/CSV downloads and email.

This Space serves a **snapshot** of the database. Sign in with the password shared with you.

Configure in **Settings → Variables and secrets**:

| Name | Kind | Purpose |
|---|---|---|
| `DASHBOARD_PASSWORD` | secret | **required** — the sign-in password |
| `DEMO_MODE` | variable | `true` — shows the Demo badge and applies the limits below |
| `DEMO_DAILY_ANALYSES` | variable | e.g. `30` — LTM analyses allowed per day |
| `DEMO_EMAIL_DOMAINS` | variable | e.g. `ltm.com` — emails only to these domains |
| `OPENAI_API_KEY` | secret | for "Analyse for LTM" and report summaries |
| `SERPER_API_KEY` | secret | company-profile search used by the analysis |
| `GMAIL_USER`, `GMAIL_APP_PASSWORD` | secrets | to email reports (optional) |

Changes made in the demo (new analyses) last until the Space restarts.
"""

SPACE_DOCKERIGNORE = """.env
**/__pycache__/
*.pyc
"""


def build(out: Path, include_gccindex_only: bool) -> dict:
    from gcc_agents import db

    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    for name in CODE:
        src = ROOT / name
        if src.is_dir():
            shutil.copytree(src, out / name, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        else:
            shutil.copy2(src, out / name)
    (out / "README.md").write_text(SPACE_README, encoding="utf-8")
    (out / ".dockerignore").write_text(SPACE_DOCKERIGNORE, encoding="utf-8")
    stats = db.export_demo(out / "data" / "gcc.db", include_gccindex_only=include_gccindex_only)
    for forbidden in (".env", "data/gccindex_state.json", "data/run.log"):
        assert not (out / forbidden).exists(), forbidden
    return stats


def upload(out: Path, space: str) -> str:
    try:
        from huggingface_hub import HfApi
    except ImportError:
        raise SystemExit("Install the uploader first:  .venv\\Scripts\\python -m pip install huggingface_hub")
    api = HfApi(token=os.getenv("HF_TOKEN") or None)
    api.create_repo(space, repo_type="space", space_sdk="docker", private=True, exist_ok=True)
    api.upload_folder(folder_path=str(out), repo_id=space, repo_type="space",
                      commit_message="Deploy LTM GCC dashboard demo")
    return f"https://huggingface.co/spaces/{space}"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default=str(ROOT / "dist" / "hf-space"), help="output folder")
    ap.add_argument("--space", help="upload to this Space, e.g. your-name/ltm-gcc-radar (created private)")
    ap.add_argument("--include-gccindex-only", action="store_true",
                    help="keep GCCs found only through GCC Index (check its terms first)")
    args = ap.parse_args()
    out = Path(args.out)
    stats = build(out, args.include_gccindex_only)
    print(f"Space folder ready: {out}")
    print(f"  demo database: {stats['kept']} GCCs ({stats['dropped_gccindex_only']} GCC Index-only rows left out)")
    if args.space:
        url = upload(out, args.space)
        print(f"Uploaded (private): {url}")
        print("Next: open the Space -> Settings -> Variables and secrets, add DASHBOARD_PASSWORD (required),"
              " DEMO_MODE=true, OPENAI_API_KEY, and optionally SERPER_API_KEY, GMAIL_USER, GMAIL_APP_PASSWORD,"
              " DEMO_EMAIL_DOMAINS, DEMO_DAILY_ANALYSES. The Space restarts and builds (about 5 minutes).")
    else:
        print("To upload:  python scripts/build_hf_space.py --space YOUR_HF_NAME/ltm-gcc-radar")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
