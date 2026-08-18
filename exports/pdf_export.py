"""Generates a simple PDF summary report of exported invoices."""
from pathlib import Path
from reportlab.lib.pagesizes import letter, landscape
from reportlab.lib import colors
from reportlab.lib.units import inch
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, Image, PageBreak
from reportlab.lib.styles import getSampleStyleSheet
from config.settings import settings
from exports.common import EXPORT_COLUMNS, invoice_export_row, invoice_totals_row


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
    totals = invoice_totals_row(invoices)
    data.append([str(totals[col]) for col in EXPORT_COLUMNS])

    table = Table(data, repeatRows=1)
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1F4E78")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTSIZE", (0, 0), (-1, -1), 6),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F2F2F2")]),
        # TOTAL row (last row) — bold + highlighted, overrides the striping above.
        ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),
        ("BACKGROUND", (0, -1), (-1, -1), colors.HexColor("#E4E9E3")),
    ]))
    elements.append(table)

    if chart_path:
        elements.append(PageBreak())
        elements.append(Paragraph("Summary Chart", styles["Heading2"]))
        elements.append(Spacer(1, 12))
        # Landscape letter usable width is ~10in; keep the chart within that.
        elements.append(Image(chart_path, width=10 * inch, height=6 * inch, kind="proportional"))

    # One mini-table per invoice that actually has line items — invoices
    # with none are skipped entirely rather than showing an empty table.
    invoices_with_items = [inv for inv in invoices if inv.get("line_items")]
    if invoices_with_items:
        elements.append(PageBreak())
        elements.append(Paragraph("Items Purchased", styles["Heading1"]))
        elements.append(Spacer(1, 12))
        for inv in invoices_with_items:
            elements.append(Paragraph(
                f"Invoice {inv.get('invoice_number')} — {inv.get('vendor_name') or 'Unknown vendor'}",
                styles["Heading3"],
            ))
            li_data = [["Description", "Quantity", "Unit Price", "Total Unit Price"]]
            for li in inv["line_items"]:
                li_data.append([
                    li.get("description") or "-",
                    f"{(li.get('quantity') or 0):,.2f}",
                    f"{(li.get('unit_price') or 0):,.2f}",
                    f"{(li.get('amount') or 0):,.2f}",
                ])
            li_table = Table(li_data, repeatRows=1, colWidths=[4 * inch, 1.5 * inch, 1.5 * inch, 1.8 * inch])
            li_table.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1F4E78")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("FONTSIZE", (0, 0), (-1, -1), 8),
                ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F2F2F2")]),
            ]))
            elements.append(li_table)
            elements.append(Spacer(1, 18))

    doc.build(elements)
    return [str(out_path)]
