"""Rebuild docs/CODE_WALKTHROUGH.html from docs/CODE_WALKTHROUGH.md (LTM look, Mermaid diagrams).

    python docs/build_html.py

Needs the `markdown` package (dev only: pip install markdown). The HTML loads Mermaid from a CDN, so the
diagrams need an internet connection when the page is opened.
"""
import pathlib
import re

import markdown

DOCS = pathlib.Path(__file__).resolve().parent
SRC, OUT = DOCS / "CODE_WALKTHROUGH.md", DOCS / "CODE_WALKTHROUGH.html"

PAGE = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>India GCC Intelligence - Code Walkthrough</title>
<style>
  :root {{ --bg:#EEF0F3; --card:#fff; --ink:#0B0B0C; --muted:#5B6470; --line:#E5E7EB; --accent:#C2391F; --coral:#F2553D; --code:#F3F4F6; }}
  @media (prefers-color-scheme: dark) {{ :root {{ --bg:#0f1114; --card:#17191E; --ink:#E6E8EB; --muted:#9AA1AB; --line:#2A2F36; --accent:#F2553D; --coral:#F2553D; --code:#1F2329; }} }}
  body {{ margin:0; background:var(--bg); color:var(--ink); font:16px/1.65 "Segoe UI",Helvetica,Arial,sans-serif; }}
  header {{ background:#0B0B0C; color:#fff; padding:18px 24px; }}
  header .logo {{ font-size:28px; font-weight:800; color:#F2553D; letter-spacing:1px; }}
  header .t {{ font-size:12px; letter-spacing:2px; text-transform:uppercase; color:#F2553D; font-weight:700; margin-top:4px; }}
  main {{ max-width:920px; margin:0 auto; padding:24px 20px 80px; background:var(--card); }}
  h1 {{ font-size:2rem; line-height:1.25; margin-top:.3em; }}
  h2 {{ margin-top:2.4em; padding-top:.6em; border-top:3px solid var(--coral); }}
  h3 {{ margin-top:2em; }} a {{ color:var(--accent); }}
  code {{ background:var(--code); padding:.12em .35em; border-radius:4px; font-size:.9em; }}
  pre {{ background:var(--code); padding:14px; border-radius:8px; overflow-x:auto; }} pre code {{ background:none; padding:0; }}
  table {{ border-collapse:collapse; width:100%; display:block; overflow-x:auto; margin:1em 0; font-size:.94em; }}
  th, td {{ border:1px solid var(--line); padding:8px 10px; text-align:left; vertical-align:top; }}
  th {{ background:#0B0B0C; color:#fff; }}
  blockquote {{ margin:1em 0; padding:.4em 1em; border-left:4px solid var(--coral); background:var(--code); }}
  hr {{ border:0; border-top:1px solid var(--line); margin:2em 0; }}
  .mermaid {{ text-align:center; margin:1.4em 0; overflow-x:auto; }}
</style></head>
<body><header><div class="logo">LTM</div><div class="t">GCC Origin &middot; India GCC Intelligence</div></header>
<main>{body}</main>
<script src="https://cdn.jsdelivr.net/npm/mermaid@10/dist/mermaid.min.js"></script>
<script>mermaid.initialize({{startOnLoad:true, theme: matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'default', flowchart:{{useMaxWidth:true}}}});</script>
</body></html>"""


def build() -> None:
    src = SRC.read_text(encoding="utf-8")
    blocks: list[str] = []

    def stash(m: re.Match) -> str:
        blocks.append(m.group(1))
        return f"\n\nMERMAIDPLACEHOLDER{len(blocks) - 1}\n\n"

    body = markdown.markdown(re.sub(r"```mermaid\n(.*?)```", stash, src, flags=re.S),
                             extensions=["tables", "fenced_code", "toc", "sane_lists"])
    for i, b in enumerate(blocks):
        body = body.replace(f"<p>MERMAIDPLACEHOLDER{i}</p>", f'<div class="mermaid">{b}</div>')
    OUT.write_text(PAGE.format(body=body), encoding="utf-8")
    print(f"wrote {OUT} ({len(blocks)} diagrams)")


if __name__ == "__main__":
    build()
