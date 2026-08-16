#!/usr/bin/env python3
"""
Build a one-page Quick Start PDF for DepthSight clients.

Renders the content from `DepthSight - Client Onboarding Materials.md` §2
into a printable A4 / Letter PDF using reportlab.

Output:
  - docs/quick-start.pdf        (1-page client deliverable)
  - docs/quick-start.html       (for browser preview / email)
"""
from __future__ import annotations
import os
from pathlib import Path
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.lib.colors import HexColor
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, KeepTogether, Table, TableStyle, PageBreak
)
from reportlab.lib.enums import TA_LEFT, TA_CENTER

# Brand palette (matches frontend indigo theme)
INDIGO_700 = HexColor("#3730a3")  # primary
INDIGO_500 = HexColor("#6366f1")  # accent
SLATE_900 = HexColor("#0f172a")
SLATE_700 = HexColor("#334155")
SLATE_500 = HexColor("#64748b")
SLATE_200 = HexColor("#e2e8f0")
SLATE_50  = HexColor("#f8fafc")
SUCCESS_500 = HexColor("#10b981")
AMBER_500 = HexColor("#f59e0b")

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
OUT_DIR = REPO / "docs"
OUT_DIR.mkdir(parents=True, exist_ok=True)
PDF_PATH = OUT_DIR / "quick-start.pdf"
HTML_PATH = OUT_DIR / "quick-start.html"


def build_styles():
    base = getSampleStyleSheet()
    styles = {
        "title": ParagraphStyle(
            "title", parent=base["Title"],
            fontName="Helvetica-Bold", fontSize=24, leading=28,
            textColor=SLATE_900, alignment=TA_LEFT, spaceAfter=4,
        ),
        "subtitle": ParagraphStyle(
            "subtitle", parent=base["Normal"],
            fontName="Helvetica", fontSize=11, leading=14,
            textColor=SLATE_500, alignment=TA_LEFT, spaceAfter=14,
        ),
        "step_num": ParagraphStyle(
            "step_num", parent=base["Normal"],
            fontName="Helvetica-Bold", fontSize=11, leading=13,
            textColor=INDIGO_700,
        ),
        "step_title": ParagraphStyle(
            "step_title", parent=base["Normal"],
            fontName="Helvetica-Bold", fontSize=12, leading=15,
            textColor=SLATE_900, spaceAfter=2,
        ),
        "step_body": ParagraphStyle(
            "step_body", parent=base["Normal"],
            fontName="Helvetica", fontSize=9.5, leading=12.5,
            textColor=SLATE_700,
        ),
        "step_inline": ParagraphStyle(
            "step_inline", parent=base["Normal"],
            fontName="Courier", fontSize=8.5, leading=11,
            textColor=SLATE_700,
        ),
        "footer": ParagraphStyle(
            "footer", parent=base["Normal"],
            fontName="Helvetica", fontSize=8, leading=10,
            textColor=SLATE_500, alignment=TA_CENTER,
        ),
        "cta": ParagraphStyle(
            "cta", parent=base["Normal"],
            fontName="Helvetica-Bold", fontSize=11, leading=14,
            textColor=INDIGO_700, alignment=TA_CENTER, spaceBefore=4, spaceAfter=4,
        ),
    }
    return styles


# (number, title, body, inline_code_optional)
STEPS = [
    (1, "Sign in (30 sec)",
     "Go to <b>https://depthsight.diverseinc.net/login</b>. Use the email/password from your welcome email, or click <b>Continue with Google</b>."),
    (2, "Open the Hub (10 sec)",
     "Click <b>Hub</b> in the left sidebar. You'll see 7 verified strategy templates &mdash; each is a complete, working strategy you can use as-is."),
    (3, "Pick one and import (30 sec)",
     "Click <b>Use Template</b> on any strategy. <b>RSI Breakout v2</b> is the most popular starting point. The strategy loads in the editor with all the configuration done for you."),
    (4, "Try the AI Co-pilot (1 min)",
     "In the editor, find the AI Co-pilot panel. Type a strategy idea in plain English and click <b>Generate</b>. You'll get a complete strategy config in 3-8 seconds. Apply it to your editor."),
    (5, "Run on paper trading (instant)",
     "At the top of the editor, set mode = <b>Paper</b> (no real money). Click <b>Start</b>. The strategy runs against live market data with <b>$10,000 fake USDT</b>. Watch it in the dashboard."),
    (6, "Connect exchange (when ready, 2 min)",
     "Settings &rarr; <b>API Keys</b> &rarr; <b>Add Exchange Key</b> &rarr; Binance or Bybit. Create a key on the exchange with <b>read + trade</b> permission (NOT withdraw), paste it here. The system validates it in seconds."),
    (7, "Switch to live (when confident)",
     "In the editor, switch from <b>Paper</b> to <b>Live</b>. Click <b>Start</b>. Real trades on the real exchange. Start small (2-5% position size) and scale up as the strategy proves itself."),
]


def _step_cell(num: int, title: str, body: str, styles):
    badge = Paragraph(f"<b>{num}</b>", styles["step_num"])
    title_p = Paragraph(title, styles["step_title"])
    body_p = Paragraph(body, styles["step_body"])
    inner = Table(
        [[badge], [title_p], [body_p]],
        colWidths=[3.3 * inch],
    )
    inner.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), SLATE_50),
        ("BOX", (0, 0), (-1, -1), 0.5, SLATE_200),
        ("LEFTPADDING", (0, 0), (-1, -1), 10),
        ("RIGHTPADDING", (0, 0), (-1, -1), 10),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
    ]))
    return inner


def build_pdf():
    styles = build_styles()
    doc = SimpleDocTemplate(
        str(PDF_PATH),
        pagesize=LETTER,
        leftMargin=0.6 * inch,
        rightMargin=0.6 * inch,
        topMargin=0.55 * inch,
        bottomMargin=0.45 * inch,
        title="DepthSight Quick Start",
        author="DepthSight",
    )
    flow = []

    # --- Header band ---
    header = Table(
        [[
            Paragraph("<b>DepthSight</b>", ParagraphStyle(
                "brand", fontName="Helvetica-Bold", fontSize=18,
                textColor=INDIGO_700, leading=20, alignment=TA_LEFT,
            )),
            Paragraph("Quick Start", ParagraphStyle(
                "tag", fontName="Helvetica", fontSize=11,
                textColor=SLATE_500, leading=14, alignment=2,
            )),
        ]],
        colWidths=[3.7 * inch, 3.7 * inch],
    )
    header.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
    ]))
    flow.append(header)

    # Divider
    divider = Table([[""]], colWidths=[7.4 * inch], rowHeights=[2])
    divider.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), INDIGO_500),
        ("LINEABOVE", (0, 0), (-1, -1), 1.5, INDIGO_500),
    ]))
    flow.append(divider)
    flow.append(Spacer(1, 0.10 * inch))

    # --- Title block ---
    flow.append(Paragraph("From signup to your first strategy in 5 minutes", styles["title"]))
    flow.append(Paragraph(
        "Seven steps. No code. No spreadsheets. Test on paper money first, then go live when you're ready.",
        styles["subtitle"],
    ))

    # --- Step cards in 2-column grid: 4 rows x 2 cols = 8 cells, last is CTA ---
    col_w = 3.65 * inch
    rows = []
    for i in range(0, len(STEPS), 2):
        left = STEPS[i]
        right = STEPS[i + 1] if i + 1 < len(STEPS) else None
        if right:
            rows.append([
                _step_cell(*left, styles=styles),
                _step_cell(*right, styles=styles),
            ])
        else:
            rows.append([_step_cell(*left, styles=styles), ""])
    grid = Table(rows, colWidths=[col_w, col_w], hAlign="LEFT")
    grid.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    flow.append(grid)
    flow.append(Spacer(1, 0.10 * inch))

    # --- Bottom CTA strip ---
    cta_table = Table(
        [[Paragraph(
            "&nbsp;That's it. You're trading.&nbsp;",
            ParagraphStyle(
                "ctamain", fontName="Helvetica-Bold", fontSize=13,
                textColor=HexColor("#ffffff"), alignment=TA_CENTER, leading=16,
            ),
        )]],
        colWidths=[7.4 * inch], rowHeights=[0.32 * inch],
    )
    cta_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), INDIGO_700),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("BOX", (0, 0), (-1, -1), 0, INDIGO_700),
    ]))
    flow.append(cta_table)
    flow.append(Spacer(1, 0.08 * inch))

    # --- Footer ---
    flow.append(Paragraph(
        "Questions? Reply to your welcome email or open a support ticket at "
        "<b>https://depthsight.diverseinc.net/support</b>",
        styles["footer"],
    ))
    flow.append(Paragraph(
        "Full documentation: <b>https://depthsight.diverseinc.net/docs</b> &nbsp;|&nbsp; "
        "Demo account: <font face='Courier'>alex_trader</font> / <font face='Courier'>DemoPassword123!</font>",
        styles["footer"],
    ))

    def _on_page(canvas, _doc):
        canvas.saveState()
        canvas.setFont("Helvetica", 7)
        canvas.setFillColor(SLATE_500)
        canvas.drawString(0.6 * inch, 0.25 * inch, "DepthSight \u2014 Quick Start")
        canvas.drawRightString(LETTER[0] - 0.6 * inch, 0.25 * inch, "depthsight.diverseinc.net")
        canvas.restoreState()

    doc.build(flow, onFirstPage=_on_page, onLaterPages=_on_page)
    return PDF_PATH


def build_html_preview():
    """Also write a simple HTML version for browser preview / email body."""
    rows = []
    for num, title, body in STEPS:
        rows.append(f"""
        <div class="step">
          <div class="num">{num}</div>
          <div>
            <h3>{title}</h3>
            <p>{body}</p>
          </div>
        </div>
        """)
    grid_html = "\n".join(rows)
    html = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<title>DepthSight \u2014 Quick Start</title>
<style>
  body {{
    font-family: -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
    background: #f8fafc; color: #0f172a; max-width: 820px; margin: 32px auto;
    padding: 32px 40px; background: #fff; border-radius: 12px;
    box-shadow: 0 1px 3px rgba(0,0,0,.06);
  }}
  h1 {{
    font-size: 28px; color: #0f172a; margin: 0 0 4px;
    border-bottom: 3px solid #6366f1; padding-bottom: 8px;
  }}
  .tag {{
    color: #64748b; font-size: 13px; margin-bottom: 18px;
  }}
  .grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 10px; margin: 16px 0 20px; }}
  .step {{
    display: flex; gap: 10px; align-items: flex-start;
    background: #f8fafc; border: 1px solid #e2e8f0;
    padding: 10px 12px; border-radius: 6px;
  }}
  .num {{
    background: #3730a3; color: #fff; border-radius: 50%;
    width: 26px; height: 26px; display: flex; align-items: center;
    justify-content: center; font-weight: 700; font-size: 13px;
    flex-shrink: 0;
  }}
  .step h3 {{
    margin: 0 0 4px; font-size: 14px; color: #0f172a;
  }}
  .step p {{
    margin: 0; font-size: 13px; line-height: 1.5; color: #334155;
  }}
  .cta {{
    background: #3730a3; color: #fff; text-align: center;
    padding: 12px; border-radius: 8px; font-weight: 700;
    font-size: 15px; margin-top: 18px;
  }}
  .footer {{
    margin-top: 16px; text-align: center; font-size: 12px; color: #64748b;
  }}
  code {{ background: #f1f5f9; padding: 1px 5px; border-radius: 3px; font-size: 12px; }}
</style>
</head>
<body>
  <h1>DepthSight \u2014 Quick Start</h1>
  <p class="tag">From signup to your first strategy in 5 minutes</p>
  <p style="font-size:14px; color:#334155; margin:0 0 16px;">
    Seven steps. No code. No spreadsheets. Test on paper money first, then go live when you're ready.
  </p>
  <div class="grid">
    {grid_html}
  </div>
  <div class="cta">That&rsquo;s it. You&rsquo;re trading.</div>
  <p class="footer">
    Questions? Reply to your welcome email or open a support ticket at
    <b>https://depthsight.diverseinc.net/support</b><br />
    Full docs: <b>https://depthsight.diverseinc.net/docs</b> &middot;
    Demo: <code>alex_trader</code> / <code>DemoPassword123!</code>
  </p>
</body>
</html>
"""
    HTML_PATH.write_text(html, encoding="utf-8")
    return HTML_PATH


def main():
    pdf = build_pdf()
    html = build_html_preview()
    print(f"PDF:  {pdf} ({pdf.stat().st_size:,} bytes)")
    print(f"HTML: {html} ({html.stat().st_size:,} bytes)")


if __name__ == "__main__":
    main()
