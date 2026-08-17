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
    # Office furniture (tables, chairs, cabinets, ...) AND PC/IT equipment
    # (units, peripherals, components) both land here — most small offices
    # buy both from the same kind of supplier, and the feedback that asked
    # for this category gave "tables/chairs" and "office PC" as one example
    # set, not two.
    "Office Supplies": [
        "office chair", "office table", "office desk", "filing cabinet",
        "steel cabinet", "bookshelf", "furniture", "desk", "table", "chair",
        "cabinet", "office depot", "office warehouse", "national bookstore",
        "computer", "laptop", "printer", "monitor", "keyboard", "mouse",
        "cpu", "pc worx", "cd-r king", "dynaquest", "complink",
        "octagon", "pc express", "villman", "ram", "ssd", "hard drive",
        "ups", "uninterruptible power supply", "router", "scanner",
        "photocopier", "toner", "ink cartridge",
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
