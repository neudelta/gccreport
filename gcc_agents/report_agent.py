"""Build the email: subject, HTML, plain-text fallback and the sector pie chart (PNG bytes)."""
import io
from collections import Counter
from dataclasses import dataclass
from datetime import date
from typing import Optional

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from jinja2 import Environment, FileSystemLoader, select_autoescape  # noqa: E402
from langchain_core.messages import HumanMessage, SystemMessage  # noqa: E402

from .config import ROOT, Settings  # noqa: E402
from .llm import chat  # noqa: E402
from .models import GCC  # noqa: E402

CHART_CID = "sector_pie"
PALETTE = ["#12355b", "#1b7f5c", "#b26a00", "#7a3e9d", "#c0392b", "#2a8bb6", "#8a97a5"]

_env = Environment(
    loader=FileSystemLoader(str(ROOT / "gcc_agents" / "templates")),
    autoescape=select_autoescape(["html", "j2"]),
)


@dataclass
class Report:
    subject: str
    html: str
    text: str
    chart_png: Optional[bytes]
    chart_cid: Optional[str]


def sector_pie(items: list[GCC]) -> Optional[bytes]:
    counts = Counter(i.sector or "Other" for i in items)
    if not counts:
        return None
    labels = [f"{k} ({v})" for k, v in counts.most_common()]
    values = [v for _, v in counts.most_common()]
    # Near-square with the legend underneath: it stays readable when a phone scales the image down to ~330px.
    fig, ax = plt.subplots(figsize=(4.6, 4.9), dpi=140)
    ax.pie(
        values, colors=PALETTE[: len(values)], startangle=90, counterclock=False,
        wedgeprops={"linewidth": 1.5, "edgecolor": "white"},
        autopct=lambda p: f"{p:.0f}%" if p >= 8 else "", pctdistance=0.72,
        textprops={"color": "white", "fontsize": 13, "fontweight": "bold"},
    )
    ax.legend(labels, loc="upper center", bbox_to_anchor=(0.5, -0.02), ncol=2, frameon=False, fontsize=12)
    ax.axis("equal")
    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight", facecolor="white")
    plt.close(fig)
    return buf.getvalue()


def _fallback_summary(items: list[GCC]) -> str:
    new = sum(i.event_type == "new_gcc" for i in items)
    exp = len(items) - new
    cities = sorted({i.city for i in items})
    return f"{new} new GCC(s) and {exp} expansion(s) across {len(cities)} location(s): {', '.join(cities)}."


def executive_summary(settings: Settings, items: list[GCC]) -> str:
    if not items:
        return ""
    facts = "\n".join(
        f"- {i.company} | {i.city} | {i.sector} | {i.event_type} | {i.status} | {i.event_date} | {i.summary}"
        for i in items
    )
    try:
        reply = chat(settings, temperature=0.2).invoke(
            [
                SystemMessage(
                    content="Write a 2-3 sentence executive summary of this week's India GCC activity for a "
                    "business email. Use ONLY the facts given; no speculation, no new numbers, no bullet points."
                ),
                HumanMessage(content=facts),
            ]
        )
        return str(reply.content).strip() or _fallback_summary(items)
    except Exception:
        return _fallback_summary(items)


def build(
    settings: Settings,
    items: list[GCC],
    window_start: date,
    window_end: date,
    already_reported_count: int,
    source_status: dict,
) -> Report:
    items = sorted(items, key=lambda i: (i.event_type != "new_gcc", i.event_date or date.min), reverse=False)
    new_count = sum(i.event_type == "new_gcc" for i in items)
    expansion_count = len(items) - new_count
    fmt = lambda d: d.strftime("%d %b %Y")  # noqa: E731
    span = f"{fmt(window_start)} – {fmt(window_end)}"

    if items:
        subject = f"GCC India Weekly: {new_count} new GCC{'s' if new_count != 1 else ''}, " \
                  f"{expansion_count} expansion{'s' if expansion_count != 1 else ''} ({span})"
        summary = executive_summary(settings, items)
    else:
        subject = f"GCC India Weekly: no new GCC activity ({span})"
        summary = (
            f"No new GCC launches or expansions in India were found between "
            f"{fmt(window_start)} and {fmt(window_end)}."
        )

    chart = sector_pie(items)
    html = _env.get_template("report.html.j2").render(
        subject=subject, items=items, summary=summary, window_start=fmt(window_start),
        window_end=fmt(window_end), new_count=new_count, expansion_count=expansion_count,
        city_count=len({i.city_key for i in items}), chart_cid=CHART_CID if chart else None,
        already_reported_count=already_reported_count,
    )

    lines = [subject, "", summary, ""]
    for i in items:
        kind = "New GCC" if i.event_type == "new_gcc" else "Expansion"
        note = " (announced)" if i.status != "opened" else ""
        lines += [f"- {i.company} | {i.city} | {i.sector} | {kind}{note} | {i.event_date}",
                  f"  {i.source_label}{' (' + i.source_host + ')' if i.source_host else ''}: {i.source_url}"]
    if already_reported_count:
        lines += ["", f"{already_reported_count} previously reported item(s) excluded."]
    return Report(subject=subject, html=html, text="\n".join(lines), chart_png=chart,
                  chart_cid=CHART_CID if chart else None)
