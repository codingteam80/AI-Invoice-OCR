"""Exports invoices to CSV."""
from pathlib import Path
import pandas as pd
from config.settings import settings
from exports.common import EXPORT_COLUMNS, invoice_export_row, invoice_totals_row, LINE_ITEM_COLUMNS, invoice_line_items_rows


def export_to_csv(invoices: list[dict], filename: str = "invoices_export.csv", chart_path: str | None = None) -> list[str]:
    rows = [invoice_export_row(inv) for inv in invoices]
    rows.append(invoice_totals_row(invoices))
    df = pd.DataFrame(rows, columns=EXPORT_COLUMNS)
    out_path = Path(settings.EXPORT_DIR) / filename
    df.to_csv(out_path, index=False)

    # CSV is plain text — there's no way to embed an image or a second
    # sheet inside it, so line items (if any) and the chart (if any) are
    # both returned as separate files alongside the main CSV instead.
    paths = [str(out_path)]

    li_rows = invoice_line_items_rows(invoices)
    if li_rows:
        li_df = pd.DataFrame(li_rows, columns=LINE_ITEM_COLUMNS)
        li_path = out_path.with_name(f"{out_path.stem}_line_items.csv")
        li_df.to_csv(li_path, index=False)
        paths.append(str(li_path))

    if chart_path:
        paths.append(chart_path)
    return paths
