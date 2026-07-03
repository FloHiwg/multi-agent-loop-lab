from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.enums import TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)


ROOT = Path(__file__).resolve().parents[1]
AUDIT = ROOT / "audits" / "audit-2025-q4-northstar"
VAULT = ROOT / "vault" / "northstar"

NAVY = colors.HexColor("#17324D")
TEAL = colors.HexColor("#1E7A78")
PALE = colors.HexColor("#EAF3F2")
INK = colors.HexColor("#24313D")
MUTED = colors.HexColor("#65737E")
LINE = colors.HexColor("#D7E0E5")

styles = getSampleStyleSheet()
styles.add(ParagraphStyle(name="CoverTitle", parent=styles["Title"], fontName="Helvetica-Bold", fontSize=25, leading=29, textColor=NAVY, spaceAfter=8))
styles.add(ParagraphStyle(name="Kicker", parent=styles["Normal"], fontName="Helvetica-Bold", fontSize=9, leading=11, textColor=TEAL, spaceAfter=5))
styles.add(ParagraphStyle(name="Sub", parent=styles["Normal"], fontSize=11, leading=16, textColor=MUTED, spaceAfter=14))
styles.add(ParagraphStyle(name="H1x", parent=styles["Heading1"], fontName="Helvetica-Bold", fontSize=15, leading=19, textColor=NAVY, spaceBefore=10, spaceAfter=8))
styles.add(ParagraphStyle(name="Bodyx", parent=styles["BodyText"], fontSize=9.5, leading=14, textColor=INK, spaceAfter=8))
styles.add(ParagraphStyle(name="Smallx", parent=styles["BodyText"], fontSize=7.5, leading=10, textColor=MUTED))


def footer(canvas, doc):
    canvas.saveState()
    canvas.setStrokeColor(LINE)
    canvas.line(18 * mm, 14 * mm, 192 * mm, 14 * mm)
    canvas.setFont("Helvetica", 7.5)
    canvas.setFillColor(MUTED)
    canvas.drawString(18 * mm, 9 * mm, "Northstar Analytics GmbH | Confidential | Synthetic evaluation fixture")
    canvas.drawRightString(192 * mm, 9 * mm, f"Page {doc.page}")
    canvas.restoreState()


def doc(path, title, subtitle, doc_id, story):
    path.parent.mkdir(parents=True, exist_ok=True)
    d = SimpleDocTemplate(str(path), pagesize=A4, rightMargin=18 * mm, leftMargin=18 * mm, topMargin=19 * mm, bottomMargin=20 * mm, title=title, author="Northstar Analytics GmbH")
    lead = [
        Paragraph("NORTHSTAR ANALYTICS", styles["Kicker"]),
        Paragraph(title, styles["CoverTitle"]),
        Paragraph(subtitle, styles["Sub"]),
        meta_table([["DOCUMENT ID", doc_id], ["REPORTING PERIOD", "Q4 2025"], ["ISSUED", "16 January 2026"]]),
        Spacer(1, 8 * mm),
    ]
    d.build(lead + story, onFirstPage=footer, onLaterPages=footer)


def meta_table(rows):
    t = Table(rows, colWidths=[42 * mm, 125 * mm], hAlign="LEFT")
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (0, -1), PALE), ("TEXTCOLOR", (0, 0), (0, -1), TEAL),
        ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"), ("FONTNAME", (1, 0), (1, -1), "Helvetica"),
        ("FONTSIZE", (0, 0), (-1, -1), 8), ("TEXTCOLOR", (1, 0), (1, -1), INK),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6), ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("LINEBELOW", (0, 0), (-1, -1), 0.4, LINE),
    ]))
    return t


def data_table(rows, widths, right_cols=()):
    t = Table(rows, colWidths=widths, repeatRows=1, hAlign="LEFT")
    cmds = [
        ("BACKGROUND", (0, 0), (-1, 0), NAVY), ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"), ("FONTNAME", (0, 1), (-1, -1), "Helvetica"),
        ("FONTSIZE", (0, 0), (-1, -1), 8), ("LEADING", (0, 0), (-1, -1), 10),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F6F8FA")]),
        ("GRID", (0, 0), (-1, -1), 0.35, LINE), ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 6), ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ]
    for col in right_cols:
        cmds.append(("ALIGN", (col, 1), (col, -1), "RIGHT"))
    t.setStyle(TableStyle(cmds))
    return t


master_story = [
    Paragraph("Executive summary", styles["H1x"]),
    Paragraph("Northstar closed Q4 with disciplined growth and continued operating leverage. Revenue was EUR 4.28 million and adjusted EBITDA reached EUR 0.74 million. Gross margin improved to 63.0%, while quarter-end cash stood at EUR 2.10 million.", styles["Bodyx"]),
    Paragraph("The organisation ended the quarter with 84 employees. Enterprise customers totalled 126 and net revenue retention was 108.0%. Average annual contract value was EUR 18.4 thousand.", styles["Bodyx"]),
    Paragraph("Customer support tickets declined 12.0% quarter over quarter. Gross logo churn was 3.1% for Q4.", styles["Bodyx"]),
    Paragraph("Headline metrics", styles["H1x"]),
    data_table([
        ["Metric", "Q4 2025 claim", "Scope"],
        ["Revenue", "EUR 4.28m", "Quarter"], ["Adjusted EBITDA", "EUR 0.74m", "Quarter"],
        ["Gross margin", "63.0%", "Quarter"], ["Cash", "EUR 2.10m", "As of 31 Dec"],
        ["Employees", "84", "As of 31 Dec"], ["Enterprise customers", "126", "As of 31 Dec"],
        ["Net revenue retention", "108.0%", "Trailing 12 months"], ["Average annual contract value", "EUR 18.4k", "As of 31 Dec"],
        ["Support ticket change", "-12.0% QoQ", "Q4 vs Q3"], ["Gross logo churn", "3.1%", "Quarter"],
    ], [68 * mm, 52 * mm, 47 * mm], right_cols=(1,)),
    Spacer(1, 5 * mm),
    Paragraph("Basis of preparation", styles["H1x"]),
    Paragraph("Amounts are management reporting figures and have not been audited. Currency amounts are presented in euros. This summary intentionally excludes underlying calculations; readers should refer to the finance pack, operations review and customer appendix.", styles["Bodyx"]),
]

finance_story = [
    Paragraph("Income statement", styles["H1x"]),
    data_table([
        ["EUR million", "Q4 2025 actual", "Q3 2025 actual", "Q4 budget"],
        ["Revenue", "4.2800", "3.9500", "4.2000"], ["Cost of revenue", "(1.5836)", "(1.5405)", "(1.5960)"],
        ["Gross profit", "2.6964", "2.4095", "2.6040"], ["Operating expenses", "(1.9564)", "(1.8995)", "(1.9640)"],
        ["Adjusted EBITDA", "0.7400", "0.5100", "0.6400"],
    ], [62 * mm, 38 * mm, 38 * mm, 38 * mm], right_cols=(1, 2, 3)),
    Spacer(1, 6 * mm),
    Paragraph("Margin bridge", styles["H1x"]),
    Paragraph("Gross margin is calculated as (Revenue - Cost of revenue) / Revenue. Using Q4 actuals: (EUR 4.2800m - EUR 1.5836m) / EUR 4.2800m = 63.0%.", styles["Bodyx"]),
    Paragraph("Liquidity", styles["H1x"]),
    data_table([
        ["EUR million", "31 Dec 2025", "30 Sep 2025"], ["Operating bank accounts", "1.7200", "1.4100"],
        ["Money market deposits", "0.3800", "0.4000"], ["Cash and cash equivalents", "2.1000", "1.8100"],
    ], [78 * mm, 48 * mm, 48 * mm], right_cols=(1, 2)),
    Spacer(1, 6 * mm),
    Paragraph("Control note", styles["H1x"]),
    Paragraph("Revenue and adjusted EBITDA reconcile to the December management ledger. Cash includes unrestricted deposits with an original maturity below three months.", styles["Bodyx"]),
]

ops_story = [
    Paragraph("People and customer base", styles["H1x"]),
    data_table([
        ["Metric", "Q4 2025", "Q3 2025", "Definition"], ["Employees", "84", "81", "Active employees at period end"],
        ["Enterprise customers", "126", "119", "Customers with enterprise plan at period end"],
        ["Net revenue retention", "106.0%", "104.0%", "Trailing 12-month cohort basis"],
    ], [50 * mm, 27 * mm, 27 * mm, 70 * mm], right_cols=(1, 2)),
    Spacer(1, 6 * mm),
    Paragraph("Support operations", styles["H1x"]),
    data_table([
        ["Metric", "Q4 2025", "Q3 2025", "Change"], ["Tickets opened", "1,100", "1,250", "-150"],
        ["Tickets resolved", "1,087", "1,228", "-141"], ["Median first response", "2.4 h", "2.8 h", "-0.4 h"],
    ], [66 * mm, 35 * mm, 35 * mm, 38 * mm], right_cols=(1, 2, 3)),
    Spacer(1, 5 * mm),
    Paragraph("Ticket volume calculation", styles["H1x"]),
    Paragraph("The quarter-over-quarter change in tickets opened is (1,100 - 1,250) / 1,250 = -12.0%.", styles["Bodyx"]),
    Paragraph("Metric governance", styles["H1x"]),
    Paragraph("People counts are maintained by People Operations. Customer counts and retention are sourced from the revenue operations snapshot dated 5 January 2026. No quarterly gross logo churn metric was approved for this review cycle.", styles["Bodyx"]),
]

doc(AUDIT / "master" / "master-report.pdf", "Q4 2025 Board Summary", "Management overview and headline claims", "northstar-master-q4-2025", master_story)
doc(VAULT / "financials-q4-2025.pdf", "Q4 2025 Finance Pack", "Management accounts and liquidity detail", "northstar-finance-q4-2025", finance_story)
doc(VAULT / "ops-metrics-q4-2025.pdf", "Q4 2025 Operations Review", "People, customer and support metrics", "northstar-ops-q4-2025", ops_story)
