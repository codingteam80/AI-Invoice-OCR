"""Keyword-based auto-categorization for invoices.

Checks BOTH the vendor name and every line item's description against each
category's keyword list. Vendor name alone only ever catches a small, fixed
set of well-known brands (utility companies, fast-food chains) — everything
else used to fall straight through to "Others". The client's own category
definitions are written in terms of what's being bought ("desks, chairs,
cabinets" / "fuel, delivery, freight"), not who the vendor is, so checking
line items is what actually makes most of these categories usable day to
day, not just for a handful of recognized brand names.
"""

# Keyword lists mix two things per category: generic product/service words
# taken straight from the client's own category definitions (comment above
# each list), plus well-known Philippine brand names where a fixed list of
# brands genuinely exists (mainly Utilities and Food, and a few big chains
# elsewhere) as a fast, high-confidence shortcut.
CATEGORY_KEYWORDS = {
    "Food": [
        # groceries, restaurant/catering, office pantry supplies
        "grocery", "groceries", "supermarket", "mini mart", "minimart",
        "sari-sari", "restaurant", "catering", "cafe", "diner", "eatery",
        "pantry", "bakery", "canteen", "coffee",
        "jollibee", "mcdo", "mcdonald", "kfc", "chowking", "greenwich",
        "starbucks", "shakey", "mang inasal", "bonchon", "jco",
    ],
    "Utilities": [
        # electricity, water, internet, telephone
        "electric bill", "electricity", "power bill", "water bill",
        "water district", "internet", "broadband", "wifi", "telephone",
        "phone bill", "postpaid plan",
        "meralco", "maynilad", "manila water", "pldt", "globe", "converge",
        "sky cable", "cignal", "smart", "dito",
    ],
    "Furnitures": [
        # desks, chairs, cabinets, fittings
        "desk", "chair", "cabinet", "fitting", "furniture", "table",
        "bookshelf", "sofa", "shelving", "filing cabinet", "steel cabinet",
    ],
    "Office Supplies": [
        # stationery, printer ink/paper, small equipment
        "stationery", "bond paper", "printer ink", "ink cartridge", "toner",
        "ballpen", "pencil", "paper clip", "folder", "envelope",
        "office supplies", "photocopier", "scanner", "calculator", "stapler",
        "national bookstore", "office depot", "office warehouse",
    ],
    "Transportation": [
        # fuel, delivery/courier, freight, vehicle maintenance
        "fuel", "gasoline", "diesel", "petron", "shell", "caltex", "seaoil",
        "phoenix petroleum", "courier", "delivery fee", "freight", "cargo",
        "lbc", "jrs express", "2go", "j&t express", "ninja van", "grab",
        "lalamove", "vehicle maintenance", "car repair", "auto repair",
        "tire", "oil change", "toll fee", "parking", "parking fee",
        "parking ticket", "parking charge", "car park",
        # Travel/ticketing services are transportation expenses when the
        # vendor/items clearly describe travel rather than lodging/tours.
        "travel agency", "travel", "air ticket", "airfare", "flight",
        "ticketing", "service fee (ticket)", "ticket fee", "booking fee",
    ],
    "Insurance": [
        # property, health, vehicle
        "insurance", "life insurance", "health insurance", "vehicle insurance",
        "car insurance", "property insurance", "insurance premium",
        "pru life", "sunlife", "philam", "axa", "manulife", "insular life",
        "malayan insurance",
    ],
    "Medical & Health Supplies": [
        # clinic/first-aid supplies
        "pharmacy", "drugstore", "mercury drug", "watsons", "clinic",
        "first aid", "first-aid", "medical supplies", "medicine", "hospital",
        "diagnostic", "st. luke's", "makati med",
        # Broader/standalone terms added after reviewing a real sample batch:
        # "medical supplies" as an exact phrase didn't match garbled OCR like
        # "medical depu" (a mangled "medical depot"), so a bare "medical"
        # keyword is needed to catch vendor names like "CGI Medical Depot"
        # that don't happen to say the exact phrase "medical supplies".
        "medical", "med depot", "medical depot", "medical center",
        "vitamins", "supplement", "softgel",
    ],
}


def auto_categorize(vendor_name: str | None, line_items: list | None = None) -> str:
    """Returns a best-guess category based on keyword matches against the
    vendor name AND every line item's description.

    Checking line items matters: the categories above are defined by what's
    being bought, not who the vendor is — a vendor's business name rarely
    spells out "we sell printer ink", but a line item description often
    does. Vendor-name-only matching (the previous behavior) only ever
    caught a handful of recognized brand names and left everything else in
    "Others".

    `line_items` accepts either LineItem objects (attribute access, as
    produced during parsing) or plain dicts (as read back from the
    database/History page) — both shapes are used across this app.

    Falls back to 'Others' if nothing matches or there's nothing to check.
    """
    haystack_parts = [vendor_name or ""]
    for li in (line_items or []):
        description = li.get("description") if isinstance(li, dict) else getattr(li, "description", None)
        if description:
            haystack_parts.append(description)

    haystack = " ".join(haystack_parts).lower()
    if not haystack.strip():
        return "Others"

    for category, keywords in CATEGORY_KEYWORDS.items():
        if any(kw in haystack for kw in keywords):
            return category

    return "Others"
