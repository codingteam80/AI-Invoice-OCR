"""Generates a fitted PDF summary report of invoice-level export data."""
from pathlib import Path
from html import escape
from reportlab.lib.pagesizes import A3, landscape
from reportlab.lib import colors
from reportlab.lib.units import mm, inch
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, Image, PageBreak
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from config.settings import settings
from exports.common import EXPORT_COLUMNS, invoice_export_row, invoice_totals_row, timestamped_export_filename

# Relative weights let long text fields breathe while guaranteeing that the
# complete 19-column table fits inside the printable A3 landscape width.
_COLUMN_WEIGHTS = {
    "ID": 0.55, "Invoice #": 0.95, "Vendor": 1.8, "Customer": 1.65, "Plate #": 0.75,
    "Invoice Date": 0.9, "Date Uploaded": 0.9, "Due Date": 0.85, "Filename": 1.55,
    "Category": 1.0, "Vatable Sales": 0.9, "VAT-Exempt Sales": 0.9, "Zero-Rated Sales": 0.9,
    "VAT": 0.7, "Withholding Tax": 0.9, "Current Charges Total": 1.0, "Previous Balance": 0.9,
    "Total Amount Due": 1.0, "Currency": 0.65,
}


def _column_widths(total_width: float) -> list[float]:
    weights = [_COLUMN_WEIGHTS.get(c, 1.0) for c in EXPORT_COLUMNS]
    scale = total_width / sum(weights)
    return [w * scale for w in weights]


def export_to_pdf(invoices: list[dict], filename: str | None = None, chart_path: str | None = None) -> list[str]:
    filename = filename or timestamped_export_filename("invoices_report", "pdf")
    out_path = Path(settings.EXPORT_DIR) / filename

    # A3 landscape + explicit fitted column widths prevents the left/right
    # clipping seen when ReportLab auto-sized the wide 19-column table.
    doc = SimpleDocTemplate(
        str(out_path), pagesize=landscape(A3),
        leftMargin=8 * mm, rightMargin=8 * mm, topMargin=10 * mm, bottomMargin=10 * mm,
    )
    styles = getSampleStyleSheet()
    cell_style = ParagraphStyle(
        "ExportCell", parent=styles["BodyText"], fontName="Helvetica", fontSize=5.2,
        leading=6.1, spaceAfter=0, spaceBefore=0,
    )
    header_style = ParagraphStyle(
        "ExportHeader", parent=cell_style, fontName="Helvetica-Bold", textColor=colors.white,
        alignment=1,
    )
    total_style = ParagraphStyle(
        "ExportTotal", parent=cell_style, fontName="Helvetica-Bold",
    )

    elements = [Paragraph("Invoice Report", styles["Title"]), Spacer(1, 8)]
    data = [[Paragraph(str(col), header_style) for col in EXPORT_COLUMNS]]
    for inv in invoices:
        row = invoice_export_row(inv)
        data.append([Paragraph(escape(str(row[col])), cell_style) for col in EXPORT_COLUMNS])
    totals = invoice_totals_row(invoices)
    data.append([Paragraph(str(totals[col]), total_style) for col in EXPORT_COLUMNS])

    table = Table(data, repeatRows=1, colWidths=_column_widths(doc.width), hAlign="CENTER")
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1F4E78")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 2),
        ("RIGHTPADDING", (0, 0), (-1, -1), 2),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("GRID", (0, 0), (-1, -1), 0.35, colors.grey),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F2F2F2")]),
        ("BACKGROUND", (0, -1), (-1, -1), colors.HexColor("#E4E9E3")),
    ]))
    elements.append(table)

    if chart_path:
        elements.append(PageBreak())
        elements.append(Paragraph("Summary Chart", styles["Heading2"]))
        elements.append(Spacer(1, 12))
        max_w = min(doc.width, 13.5 * inch)
        elements.append(Image(chart_path, width=max_w, height=max_w * 0.6, kind="proportional"))

    # v1.63: Items Purchased / Line Items are intentionally excluded.
    doc.build(elements)
    return [str(out_path)]
