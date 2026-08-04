"""Simple keyword-based auto-categorization for invoices."""

CATEGORY_KEYWORDS = {
    "Utilities": [
        "meralco", "maynilad", "manila water", "pldt", "globe", "converge",
        "sky cable", "cignal", "smart", "dito",
    ],
    "Food": [
        "jollibee", "mcdo", "mcdonald", "kfc", "chowking", "greenwich",
        "starbucks", "shakey", "mang inasal", "bonchon", "jco",
    ],
}


def auto_categorize(vendor_name: str | None) -> str:
    """Returns a best-guess category based on vendor name keywords.
    Falls back to 'Others' if nothing matches or vendor_name is empty.
    """
    if not vendor_name:
        return "Others"

    vendor_lower = vendor_name.lower()

    for category, keywords in CATEGORY_KEYWORDS.items():
        if any(kw in vendor_lower for kw in keywords):
            return category

    return "Others"
