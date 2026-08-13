"""Dispatches export requests to the correct format-specific exporter."""
from exports.excel_export import export_to_excel
from exports.csv_export import export_to_csv
from exports.pdf_export import export_to_pdf

_EXPORTERS = {
    "xlsx": export_to_excel,
    "csv": export_to_csv,
    "pdf": export_to_pdf,
}


def export_invoices(
    invoices: list[dict], fmt: str = "xlsx", filename: str | None = None, chart_path: str | None = None
) -> list[str]:
    """Returns a list of generated file paths — normally just the one
    export file, but CSV returns two when a chart is attached (see
    exports/csv_export.py; a chart image can't be embedded in plain text)."""
    fmt = fmt.lower()
    if fmt not in _EXPORTERS:
        raise ValueError(f"Unsupported export format: {fmt}")
    kwargs = {"chart_path": chart_path}
    if filename:
        kwargs["filename"] = filename
    return _EXPORTERS[fmt](invoices, **kwargs)
