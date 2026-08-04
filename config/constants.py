"""Shared constants used across the app."""

SUPPORTED_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".tiff", ".bmp"}
SUPPORTED_PDF_EXTS = {".pdf"}
SUPPORTED_EXTS = SUPPORTED_IMAGE_EXTS | SUPPORTED_PDF_EXTS

INVOICE_STATUS_PENDING = "pending"
INVOICE_STATUS_PROCESSING = "processing"
INVOICE_STATUS_PROCESSED = "processed"
INVOICE_STATUS_REVIEW = "needs_review"
INVOICE_STATUS_FAILED = "failed"

CURRENCY_SYMBOLS = {
    "$": "USD", "€": "EUR", "£": "GBP", "¥": "JPY",
    "₱": "PHP", "₹": "INR", "₩": "KRW",
}

REQUIRED_FIELDS = [
    "invoice_number", "invoice_date", "vendor_name",
    "total_amount", "currency",
]

<<<<<<< HEAD
# Fixed list of purchase categories. The LLM is asked to pick one of these
# during extraction (see ai/prompt_builder.py); anything it returns that
# isn't in this list gets normalized to "Others" (see
# ai/post_processing.normalize_category). Used to group invoices into
# separate tables on the History page.
CATEGORY_OPTIONS = ["Foods", "Office Supplies", "Furnitures", "Others"]
DEFAULT_CATEGORY = "Others"

=======
>>>>>>> 6f955363a7d26856b8f0ea3d25a45457d11dfa98
EXTRACTION_SCHEMA = {
    "invoice_number": (
        "string — the document's unique identifier. On formal invoices this "
        "is usually labeled 'Invoice Number' or 'Invoice #'. On retail "
        "receipts, look for equivalents like 'Sales Invoice Number', "
        "'SI No.', 'OR No.' (Official Receipt Number), 'Transaction #', "
        "or 'TR NO.' — use whichever such number appears on the document."
    ),
    "invoice_date": "YYYY-MM-DD",
    "due_date": "YYYY-MM-DD or null",
    "vendor_name": "string",
    "vendor_address": "string or null",
    "vendor_tax_id": "string or null",
    "customer_name": "string or null",
    "line_items": [
        {"description": "string", "quantity": "number", "unit_price": "number", "amount": "number"}
    ],
    "subtotal": (
        "number — the Net Amount: the sum of line items before tax/VAT and "
        "discount is applied. Look for labels like 'Subtotal', 'Net Amount', "
        "or 'Net Total'."
    ),
    "tax_amount": (
        "number or null — the VAT (Value Added Tax) amount added on top of "
        "the subtotal/net amount. Also look for equivalent labels like "
        "'Tax', 'GST', or 'Sales Tax' if VAT isn't explicitly used."
    ),
    "tax_rate": "number or null — the VAT/tax rate as a percentage (e.g. 12 for 12% VAT)",
    "discount": "number or null",
    "total_amount": (
        "number — the Total Amount Due: the final amount owed/payable for "
        "this transaction, i.e. the Net Amount plus VAT and any other "
        "applicable charges (also known as 'Gross Total'). Look for labels "
        "like TOTAL, AMOUNT DUE, AMOUNT PAYABLE, or GRAND TOTAL. Do NOT use "
        "CASH/TENDERED (money handed over by the customer) or CHANGE (money "
        "handed back) — those are payment mechanics, not the total."
    ),
    "currency": "3-letter ISO code",
    "payment_terms": "string or null",
<<<<<<< HEAD
    "category": (
        f"string — one of exactly: {', '.join(CATEGORY_OPTIONS)}. Classify "
        "what was purchased based on the vendor name and line items. If it "
        "doesn't clearly fit Foods, Office Supplies, or Furnitures, use "
        "'Others'."
    ),
=======
>>>>>>> 6f955363a7d26856b8f0ea3d25a45457d11dfa98
}
