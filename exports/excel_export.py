"""Exports a list of invoices to a formatted Excel workbook."""
from pathlib import Path
import pandas as pd
from openpyxl.styles import Font, PatternFill
from config.settings import settings


def export_to_excel(invoices: list[dict], filename: str = "invoices_export.xlsx") -> str:
    rows = []
    for inv in invoices:
        rows.append({
            "Invoice #": inv["invoice_number"],
            "Date": inv.get("invoice_date"),
            "Vendor": inv["vendor_name"],
            "Customer": inv.get("customer_name"),
            "Subtotal": inv.get("subtotal"),
            "Tax": inv.get("tax_amount"),
            "Discount": inv.get("discount"),
            "Total": inv.get("total_amount"),
            "Currency": inv.get("currency"),
            "Status": inv.get("status"),
            "Confidence": inv.get("confidence_score"),
        })

    df = pd.DataFrame(rows)
    out_path = Path(settings.EXPORT_DIR) / filename

    with pd.ExcelWriter(out_path, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="Invoices")
        ws = writer.sheets["Invoices"]
        header_fill = PatternFill(start_color="1F4E78", end_color="1F4E78", fill_type="solid")
        for cell in ws[1]:
            cell.font = Font(color="FFFFFF", bold=True)
            cell.fill = header_fill
        for col in ws.columns:
            max_len = max((len(str(c.value)) for c in col if c.value is not None), default=10)
            ws.column_dimensions[col[0].column_letter].width = min(max_len + 4, 40)

    return str(out_path)
