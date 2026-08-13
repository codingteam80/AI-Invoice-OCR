"""Exports invoices to CSV."""
from pathlib import Path
import pandas as pd
from config.settings import settings
from exports.common import EXPORT_COLUMNS, invoice_export_row


def export_to_csv(invoices: list[dict], filename: str = "invoices_export.csv", chart_path: str | None = None) -> list[str]:
    rows = [invoice_export_row(inv) for inv in invoices]
    df = pd.DataFrame(rows, columns=EXPORT_COLUMNS)
    out_path = Path(settings.EXPORT_DIR) / filename
    df.to_csv(out_path, index=False)

    # CSV is plain text — there's no way to embed an image inside it, so the
    # chart (if any) is returned as a second file alongside the CSV instead.
    paths = [str(out_path)]
    if chart_path:
        paths.append(chart_path)
    return paths
