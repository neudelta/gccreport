"""HTML -> PDF using the Chromium that Playwright already installs.

The cover is rendered on its own (full-bleed, no footer) and the body with page margins and a running footer
with page numbers; the two PDFs are then joined.
"""
import io

FOOTER = """<div style="width:100%;font-family:'Segoe UI',Helvetica,Arial,sans-serif;font-size:7.5pt;color:#8C939D;
padding:0 15mm;display:flex;justify-content:space-between;">
<span><b style="color:#C2391F;letter-spacing:.5px;">LTM</b>&nbsp; GCC Origin &middot; {label}</span>
<span>Internal &middot; page <span class="pageNumber"></span> of <span class="totalPages"></span></span></div>"""


def render_pdf(body_html: str, cover_html: str = "", footer_label: str = "India GCC Weekly Intel Report") -> bytes:
    from playwright.sync_api import sync_playwright
    from pypdf import PdfReader, PdfWriter

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.set_content(body_html, wait_until="load")
            body = page.pdf(
                format="A4", print_background=True, display_header_footer=True,
                header_template="<div></div>", footer_template=FOOTER.format(label=footer_label),
                margin={"top": "16mm", "bottom": "18mm", "left": "15mm", "right": "15mm"},
            )
            if not cover_html:
                return body
            page.set_content(cover_html, wait_until="load")
            cover = page.pdf(format="A4", print_background=True, margin={"top": "0", "bottom": "0",
                                                                          "left": "0", "right": "0"})
        finally:
            browser.close()

    writer = PdfWriter()
    for blob in (cover, body):
        for p in PdfReader(io.BytesIO(blob)).pages:
            writer.add_page(p)
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()
