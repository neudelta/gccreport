"""Send the report through Gmail SMTP (App Password)."""
import smtplib
import ssl
import time
from email.mime.application import MIMEApplication
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from .config import Settings
from .report_agent import Report
from .tracing import trace_tool


def build_message(settings: Settings, report: Report) -> MIMEMultipart:
    """multipart/related (HTML + inline images), wrapped in multipart/mixed when the PDF briefing is attached."""
    related = MIMEMultipart("related")
    alt = MIMEMultipart("alternative")
    alt.attach(MIMEText(report.text, "plain", "utf-8"))
    alt.attach(MIMEText(report.html, "html", "utf-8"))
    related.attach(alt)
    inline = dict(report.images)
    if report.chart_png and report.chart_cid:
        inline[report.chart_cid] = report.chart_png
    for cid, png in inline.items():
        img = MIMEImage(png, _subtype="png")
        img.add_header("Content-ID", f"<{cid}>")
        img.add_header("Content-Disposition", "inline", filename=f"{cid}.png")
        related.attach(img)

    if report.pdf:
        msg = MIMEMultipart("mixed")
        msg.attach(related)
        part = MIMEApplication(report.pdf, _subtype="pdf")
        part.add_header("Content-Disposition", "attachment", filename=report.pdf_name)
        msg.attach(part)
    else:
        msg = related
    msg["Subject"] = report.subject
    msg["From"] = settings.gmail_user
    msg["To"] = settings.to_email
    return msg


def missing_config(settings: Settings) -> list[str]:
    return [
        name for name, val in (
            ("GMAIL_USER", settings.gmail_user),
            ("GMAIL_APP_PASSWORD", settings.gmail_password),
            ("TO_EMAIL", settings.to_email),
        ) if not val
    ]


@trace_tool("send_email", process_inputs=lambda d: {"subject": d["report"].subject})
def send(settings: Settings, report: Report, retries: int = 1) -> tuple[bool, str]:
    """Returns (sent, detail). Never raises; the caller decides what a failure means."""
    missing = missing_config(settings)
    if missing:
        return False, f"missing settings: {', '.join(missing)}"
    msg = build_message(settings, report)
    last = ""
    for attempt in range(retries + 1):
        try:
            with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ssl.create_default_context(), timeout=30) as smtp:
                smtp.login(settings.gmail_user, settings.gmail_password.replace(" ", ""))
                smtp.send_message(msg)
            return True, "sent"
        except Exception as exc:
            last = f"{type(exc).__name__}: {exc}"[:200]
            if attempt < retries:
                time.sleep(3)
    return False, last
