"""Generates a simple PDF summary report of exported invoices."""
from pathlib import Path
from reportlab.lib.pagesizes import letter, landscape
from reportlab.lib import colors
from reportlab.lib.units import inch
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, Image, PageBreak
from reportlab.lib.styles import getSampleStyleSheet
from config.settings import settings
from exports.common import EXPORT_COLUMNS, invoice_export_row


def export_to_pdf(invoices: list[dict], filename: str = "invoices_report.pdf", chart_path: str | None = None) -> list[str]:
    out_path = Path(settings.EXPORT_DIR) / filename
    # Landscape + a small font, since the full column set (16 columns) is
    # too wide for portrait letter — see exports/common.py.
    doc = SimpleDocTemplate(str(out_path), pagesize=landscape(letter))
    styles = getSampleStyleSheet()
    elements = [Paragraph("Invoice Report", styles["Title"]), Spacer(1, 12)]

    data = [EXPORT_COLUMNS]
    for inv in invoices:
        row = invoice_export_row(inv)
        data.append([str(row[col]) for col in EXPORT_COLUMNS])

    table = Table(data, repeatRows=1)
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1F4E78")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTSIZE", (0, 0), (-1, -1), 6),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F2F2F2")]),
    ]))
    elements.append(table)

    if chart_path:
        elements.append(PageBreak())
        elements.append(Paragraph("Summary Chart", styles["Heading2"]))
        elements.append(Spacer(1, 12))
        # Landscape letter usable width is ~10in; keep the chart within that.
        elements.append(Image(chart_path, width=10 * inch, height=6 * inch, kind="proportional"))

    doc.build(elements)
    return [str(out_path)]
