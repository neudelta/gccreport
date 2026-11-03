"""Build the executive email (HTML + text) and the detailed briefing (PDF), both in the LTM theme.

- Email: brand bar, hero, KPIs, LTM-perspective summary, two-week overview table, top GCC Origin opportunities,
  sector chart. Images are inline (cid:) so they show without "load images".
- Briefing PDF: cover, executive overview with charts, one account profile per company (business area, products,
  India-centre mandate, how LTM wins), and a GCC Origin reference page.
"""
import base64
import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Optional

from jinja2 import Environment, FileSystemLoader, select_autoescape
from langchain_core.messages import HumanMessage, SystemMessage

from . import ltm, visuals
from .config import ROOT, Settings
from .llm import chat
from .models import GCC
from .pdf import render_pdf

log = logging.getLogger("gcc")

CHART_CID = "sector_pie"
SUBJECT_NAME = "India GCC Report"
PDF_TITLE = "India GCC Weekly Intel Report"
HERO_CID = "ltm_hero"
FIT_ORDER = {"High": 0, "Medium": 1, "Low": 2}

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
    images: dict = field(default_factory=dict)      # extra inline images: cid -> png bytes
    pdf: Optional[bytes] = None                     # the detailed briefing
    pdf_name: str = "India GCC Weekly Intel Report.pdf"
    pdf_html: str = ""                              # the briefing as HTML (kept for previews)


def _data_uri(png: Optional[bytes]) -> str:
    return "data:image/png;base64," + base64.b64encode(png).decode() if png else ""


def sector_pie(items: list[GCC]) -> Optional[bytes]:  # kept for callers/tests that import it from here
    return visuals.sector_pie(items)


def _fit_rank(i: GCC) -> int:
    return FIT_ORDER.get(i.insight.fit, 3) if i.insight else 3


def _fallback_summary(items: list[GCC]) -> str:
    new = sum(i.event_type == "new_gcc" for i in items)
    exp = len(items) - new
    cities = sorted({i.city for i in items})
    high = sum(1 for i in items if i.insight and i.insight.fit == "High")
    return (f"{new} new GCC(s) and {exp} expansion(s) across {len(cities)} location(s) ({', '.join(cities)}); "
            f"{high} rated high-fit for LTM GCC Origin.")


def executive_summary(settings: Settings, items: list[GCC]) -> str:
    if not items:
        return ""
    facts = "\n".join(  # the 60 most relevant are plenty for a 3-sentence summary
        f"- {i.company} | {i.city} | {i.sector} | {i.event_type} | {i.status} | {i.event_date} | {i.summary}"
        + (f" | LTM fit: {i.insight.fit} | play: {i.insight.engagement_model}" if i.insight else "")
        for i in sorted(items, key=_fit_rank)[:60]
    )
    try:
        reply = chat(settings, temperature=0.2).invoke([
            SystemMessage(content=(
                "Write a 3-sentence executive summary for LTM leadership about India GCC activity in the period. "
                "Sentence 1: the headline (how many new GCCs and expansions, notable names). Sentence 2: the pattern "
                "(sectors, cities and what the centres will do). Sentence 3: the implication for LTM's GCC Origin "
                "business, naming the 2-3 accounts to prioritise (the High-fit ones). The 'play' values are LTM's "
                "recommended engagement models, not the companies' plans: never describe them as company mandates. "
                "Use ONLY the facts given; no new numbers; no bullet points; no hype.")),
            HumanMessage(content=facts),
        ])
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
    make_pdf: bool = True,
    email_limit: int = 40,
    profile_limit: int = 40,
    slim: bool = False,
) -> Report:
    """`email_limit` caps rows in the email table and `profile_limit` the PDF account profiles, so a whole-year
    selection from the dashboard stays readable; the PDF overview table always lists every item.
    `slim` (dashboard emails only) uses compact row markup so long lists stay under Gmail's clipping limit;
    the weekly report keeps its original markup."""
    items = sorted(items, key=lambda i: (_fit_rank(i), i.event_type != "new_gcc",
                                         -(i.event_date.toordinal() if i.event_date else 0)))
    new_count = sum(i.event_type == "new_gcc" for i in items)
    expansion_count = len(items) - new_count
    high_count = sum(1 for i in items if i.insight and i.insight.fit == "High")
    top = [i for i in items if i.insight and i.insight.fit in ("High", "Medium")][:3]
    fmt = lambda d: d.strftime("%d %b %Y")  # noqa: E731
    span = f"{fmt(window_start)} – {fmt(window_end)}"

    if items:
        subject = (f"{SUBJECT_NAME} | {new_count} new GCC{'s' if new_count != 1 else ''} · "
                   f"{expansion_count} expansion{'s' if expansion_count != 1 else ''} · {span}")
        summary = executive_summary(settings, items)
        preheader = f"{high_count} high-fit opportunities for GCC Origin. {PDF_TITLE} attached."
    else:
        subject = f"{SUBJECT_NAME} | no new GCC activity · {span}"
        summary = f"No new GCC launches or expansions in India were found between {fmt(window_start)} and {fmt(window_end)}."
        preheader = summary

    hero = visuals.hero_wave()
    pie = visuals.sector_pie(items)
    # profiles: analysed accounts first, then new GCCs, newest first
    profiles = sorted(items, key=lambda i: (i.insight is None, _fit_rank(i), i.event_type != "new_gcc",
                                            -(i.event_date.toordinal() if i.event_date else 0)))[:profile_limit]
    common = dict(
        items=items, email_items=items[:email_limit], more_count=max(0, len(items) - email_limit),
        profiles=profiles, profiles_hidden=max(0, len(items) - len(profiles)), summary=summary, window_start=fmt(window_start), window_end=fmt(window_end),
        new_count=new_count, expansion_count=expansion_count, high_count=high_count,
        city_count=len({i.city_key for i in items}), already_reported_count=already_reported_count,
    )

    pdf_bytes, pdf_html = None, ""
    if make_pdf and items:
        brief = dict(
            title=PDF_TITLE, generated=datetime.now().strftime("%d %b %Y"),
            hero_src=_data_uri(visuals.hero_wave(width_in=8.27, height_in=3.4, seed=11)),
            pie_src=_data_uri(pie), city_src=_data_uri(visuals.city_bars(items)),
            services=ltm.SERVICES, models=ltm.ENGAGEMENT_MODELS, model_short=ltm.MODEL_SHORT, **common,
        )
        tpl = _env.get_template("briefing.html.j2")
        pdf_html = tpl.render(part="all", **brief)  # one HTML file for previews
        try:
            pdf_bytes = render_pdf(tpl.render(part="body", **brief), cover_html=tpl.render(part="cover", **brief),
                                   footer_label=f"{PDF_TITLE} &middot; {span}")
        except Exception as exc:  # the email still goes out without the attachment
            log.warning("briefing PDF could not be rendered: %s", exc)

    html = _env.get_template("report.html.j2").render(
        subject=subject, preheader=preheader, top=top, pdf_title=PDF_TITLE, hero_src=f"cid:{HERO_CID}",
        chart_src=f"cid:{CHART_CID}" if pie else "", has_pdf=pdf_bytes is not None, slim=slim, **common,
    )
    if slim:
        html = _compact(html)

    lines = [subject, "", summary, ""]
    for i in items:
        kind = "New GCC" if i.event_type == "new_gcc" else "Expansion"
        fit = f" | LTM fit: {i.insight.fit} ({i.insight.engagement_model})" if i.insight else ""
        note = " (announced)" if i.status != "opened" else ""
        lines += [f"- {i.company} | {i.city} | {i.sector} | {kind}{note} | {i.event_date}{fit}",
                  f"  {i.source_label}{' (' + i.source_host + ')' if i.source_host else ''}: {i.source_url}"
                  if i.source_url else "  Source: GCC Index (subscription data)"]
    if pdf_bytes:
        lines += ["", f"{PDF_TITLE} attached (PDF)."]
    if already_reported_count:
        lines += ["", f"{already_reported_count} previously reported item(s) excluded."]

    stamp = window_end.strftime("%Y-%m-%d")
    return Report(subject=subject, html=html, text="\n".join(lines), chart_png=pie,
                  chart_cid=CHART_CID if pie else None, images={HERO_CID: hero}, pdf=pdf_bytes,
                  pdf_name=f"{PDF_TITLE} {stamp}.pdf", pdf_html=pdf_html)


def _compact(html: str) -> str:
    """Drop indentation and blank lines from the email HTML (keeps one line break wherever there was whitespace,
    so text never runs together). Keeps long lists under Gmail's ~102 KB clipping limit."""
    return re.sub(r"\n[ \t]*(?:\n[ \t]*)*", "\n", html).strip()


def preview_html(rep: Report) -> str:
    """The email with its inline images swapped for data URIs, so a browser can show it."""
    html = rep.html
    if rep.chart_png and rep.chart_cid:
        html = html.replace(f"cid:{rep.chart_cid}", _data_uri(rep.chart_png))
    for cid, png in rep.images.items():
        html = html.replace(f"cid:{cid}", _data_uri(png))
    return html
