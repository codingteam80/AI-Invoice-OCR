"""Keep an unread amount distinct from an explicit printed zero."""
def display_money(value, currency=None):
    if value is None:
        return "Not available"
    return f"{value:,.2f} {currency or ''}".strip()
