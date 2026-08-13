"""Builds the summary bar chart shown on the Reports page and embedded in
exports. Kept separate from exports/common.py since this produces an image
file, not a row/column definition."""
from pathlib import Path
import matplotlib
matplotlib.use("Agg")  # headless — Streamlit runs with no display server
import matplotlib.pyplot as plt
from config.settings import settings

# Brand colors (see ui/components/nav.py / .streamlit/config.toml)
_BAR_COLOR = "#3AAF11"
_TEXT_COLOR = "#1A231F"

_GROUP_KEY_FUNCS = {
    "Vendor": lambda inv: inv.get("vendor_name") or "Unknown",
    "Category": lambda inv: inv.get("category") or "Others",
    "Month": lambda inv: (inv.get("invoice_date") or (inv.get("created_at") or "")[:10])[:7] or "Unknown",
}


def generate_chart(invoices: list[dict], group_by: str | list[str] = "Vendor", filename: str = "invoices_chart.png") -> str:
    """Aggregates Total Amount Due by `group_by` — one of "Vendor" |
    "Category" | "Month", or a list of several to group by a compound key
    (e.g. ["Vendor", "Category"] -> bars like "Acme / Foods") — and saves a
    bar chart PNG. Returns the file path."""
    dims = [group_by] if isinstance(group_by, str) else list(group_by)
    key_funcs = [_GROUP_KEY_FUNCS.get(d, _GROUP_KEY_FUNCS["Vendor"]) for d in dims]

    totals: dict[str, float] = {}
    for inv in invoices:
        key = " / ".join(str(f(inv)) for f in key_funcs)
        totals[key] = totals.get(key, 0) + (inv.get("total_amount") or 0)

    # Largest first, so the chart reads like a ranked summary.
    labels = sorted(totals, key=lambda k: totals[k], reverse=True)
    values = [totals[k] for k in labels]

    fig, ax = plt.subplots(figsize=(10, max(4, 0.4 * len(labels))))
    ax.barh(labels, values, color=_BAR_COLOR)
    ax.invert_yaxis()  # largest bar on top
    ax.set_xlabel("Total Amount Due", color=_TEXT_COLOR)
    ax.set_title(f"Total Amount Due by {' & '.join(dims)}", color=_TEXT_COLOR, fontsize=14, fontweight="bold")
    ax.tick_params(colors=_TEXT_COLOR)
    for spine in ("top", "right"):
        ax.spines[spine].set_visible(False)
    fig.tight_layout()

    out_path = Path(settings.EXPORT_DIR) / filename
    fig.savefig(out_path, dpi=150)
    plt.close(fig)
    return str(out_path)
