"""Generates a simple PDF summary report of exported invoices."""
from pathlib import Path
from reportlab.lib.pagesizes import letter
from reportlab.lib import colors
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
from reportlab.lib.styles import getSampleStyleSheet
from config.settings import settings


def export_to_pdf(invoices: list[dict], filename: str = "invoices_report.pdf") -> str:
    out_path = Path(settings.EXPORT_DIR) / filename
    doc = SimpleDocTemplate(str(out_path), pagesize=letter)
    styles = getSampleStyleSheet()
    elements = [Paragraph("Invoice Report", styles["Title"]), Spacer(1, 12)]

    data = [["Invoice #", "Vendor", "Date", "Total", "Currency", "Status"]]
    for inv in invoices:
        data.append([
            inv["invoice_number"], inv["vendor_name"], inv.get("invoice_date") or "-",
            f'{inv.get("total_amount", 0):.2f}', inv.get("currency", ""), inv.get("status", ""),
        ])

    table = Table(data, repeatRows=1)
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1F4E78")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTSIZE", (0, 0), (-1, -1), 8),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F2F2F2")]),
    ]))
    elements.append(table)
    doc.build(elements)
    return str(out_path)
