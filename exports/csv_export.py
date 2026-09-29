"""Exports invoice-level data to CSV."""
from pathlib import Path
import pandas as pd
from config.settings import settings
from exports.common import EXPORT_COLUMNS, invoice_export_row, invoice_totals_row, timestamped_export_filename


def export_to_csv(invoices: list[dict], filename: str | None = None, chart_path: str | None = None) -> list[str]:
    rows = [invoice_export_row(inv) for inv in invoices]
    rows.append(invoice_totals_row(invoices))
    df = pd.DataFrame(rows, columns=EXPORT_COLUMNS)
    filename = filename or timestamped_export_filename("invoices_export", "csv")
    out_path = Path(settings.EXPORT_DIR) / filename
    df.to_csv(out_path, index=False)

    # CSV cannot embed the optional chart. Line Items are intentionally not
    # exported in v1.63, so the invoice CSV is the only generated CSV file.
    return [str(out_path)]
