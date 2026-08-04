"""Dispatches export requests to the correct format-specific exporter."""
from exports.excel_export import export_to_excel
from exports.csv_export import export_to_csv
from exports.pdf_export import export_to_pdf

_EXPORTERS = {
    "xlsx": export_to_excel,
    "csv": export_to_csv,
    "pdf": export_to_pdf,
}


def export_invoices(invoices: list[dict], fmt: str = "xlsx", filename: str | None = None) -> str:
    fmt = fmt.lower()
    if fmt not in _EXPORTERS:
        raise ValueError(f"Unsupported export format: {fmt}")
    kwargs = {"filename": filename} if filename else {}
    return _EXPORTERS[fmt](invoices, **kwargs)
