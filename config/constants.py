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
    "$": "USD", "â‚¬": "EUR", "Â£": "GBP", "Â¥": "JPY",
    "â‚±": "PHP", "â‚¹": "INR", "â‚©": "KRW",
}

REQUIRED_FIELDS = [
    "invoice_number", "invoice_date", "vendor_name",
    "total_amount", "currency",
]

EXTRACTION_SCHEMA = {
    "invoice_number": (
        "string â€” the document's unique identifier. On formal invoices this "
        "is usually labeled 'Invoice Number' or 'Invoice #'. On retail "
        "receipts, look for equivalents like 'Sales Invoice Number', "
        "'SI No.', 'OR No.' (Official Receipt Number), 'Transaction #', "
        "or 'TR NO.' â€” use whichever such number appears on the document."
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
        "number â€” the Net Amount: the sum of line items before tax/VAT and "
        "discount is applied. Look for labels like 'Subtotal', 'Net Amount', "
        "or 'Net Total'."
    ),
    "tax_amount": (
        "number or null â€” the VAT (Value Added Tax) amount added on top of "
        "the subtotal/net amount. Also look for equivalent labels like "
        "'Tax', 'GST', or 'Sales Tax' if VAT isn't explicitly used."
    ),
    "tax_rate": "number or null â€” the VAT/tax rate as a percentage (e.g. 12 for 12% VAT)",
    "discount": "number or null",
    "total_amount": (
        "number â€” the Total Amount Due: the final amount owed/payable for "
        "this transaction, i.e. the Net Amount plus VAT and any other "
        "applicable charges (also known as 'Gross Total'). Look for labels "
        "like TOTAL, AMOUNT DUE, AMOUNT PAYABLE, or GRAND TOTAL. Do NOT use "
        "CASH/TENDERED (money handed over by the customer) or CHANGE (money "
        "handed back) â€” those are payment mechanics, not the total."
    ),
    "currency": "3-letter ISO code",
    "payment_terms": "string or null",
}


CATEGORY_OPTIONS = ["Utilities", "Food", "Others"]
