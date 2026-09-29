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
    """Generate the selected invoice-level export and return its file path(s).

    Default filenames are timestamped by the format-specific exporter. Items
    Purchased / Line Items are intentionally excluded from all formats.
    """
    fmt = fmt.lower()
    if fmt not in _EXPORTERS:
        raise ValueError(f"Unsupported export format: {fmt}")
    kwargs = {"chart_path": chart_path}
    if filename:
        kwargs["filename"] = filename
    return _EXPORTERS[fmt](invoices, **kwargs)
