"""Exports invoices to CSV."""
from pathlib import Path
import pandas as pd
from config.settings import settings


def export_to_csv(invoices: list[dict], filename: str = "invoices_export.csv") -> str:
    rows = [{
        "Invoice #": inv["invoice_number"],
        "Date": inv.get("invoice_date"),
        "Vendor": inv["vendor_name"],
        "Total": inv.get("total_amount"),
        "Currency": inv.get("currency"),
        "Status": inv.get("status"),
    } for inv in invoices]

    df = pd.DataFrame(rows)
    out_path = Path(settings.EXPORT_DIR) / filename
    df.to_csv(out_path, index=False)
    return str(out_path)
