"""Shared constants used across the app."""

APP_NAME = "AI Invoice OCR"
APP_VERSION = "1.0.0"
COMPANY_NAME = "Tsukiden Global Solutions, Inc."
COPYRIGHT_YEAR = "2026"

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
    "invoice_date": (
        "YYYY-MM-DD â€” the date THIS transaction/invoice was issued, usually "
        "labeled just 'Date:' near the invoice number at the top of the "
        "document. IMPORTANT: ignore dates found near BIR/permit boilerplate "
        "at the bottom of Philippine receipts, e.g. 'Dated of Issue', 'Date "
        "of ATP', or anything next to a 'Looseleaf Permit No.' / 'BIR "
        "Authority to Print No.' â€” those are the PRINTED FORM's registration "
        "date, often years earlier, not the date of this transaction."
    ),
    "due_date": "YYYY-MM-DD or null",
    "vendor_name": (
        "string â€” the SHORT business/brand name only, e.g. 'MedExpress' or "
        "'DataBlitz'. Do NOT concatenate the whole letterhead block into "
        "this field. Specifically exclude: taglines/slogans (e.g. 'The No. "
        "1 Hospital Outpatient Pharmacy'), 'Owned & Operated by:' clauses "
        "and the legal entity name that follows it, and the name of a host "
        "location the business operates inside of (e.g. a pharmacy located "
        "inside 'Manila Doctors Hospital' is still vendor 'MedExpress', not "
        "'MedExpress Manila Doctors Hospital'). If you genuinely can't tell "
        "which of several nearby lines is the brand name on a low-quality "
        "scan, pick the single most prominent/largest one â€” never join "
        "multiple lines together as a fallback."
    ),
    "vendor_address": "string or null",
    "vendor_tax_id": "string or null — the VENDOR's TIN (VAT Reg. TIN), from their own letterhead/header block.",
    "customer_name": (
        "string or null â€” the client/billed-to ORGANIZATION or COMPANY name, "
        "e.g. from a 'Client:', 'Bill To:', or 'Customer:' block. If the "
        "block lists both a company and an individual (often marked "
        "'Attn:', 'ATTN:', or 'c/o'), customer_name is the COMPANY, never "
        "the individual â€” put the individual in customer_contact instead. "
        "IMPORTANT: many retail receipts have a 'SOLD TO' / 'Registered "
        "Name' block that is left BLANK for walk-in/cash customers â€” if "
        "that block has no name actually filled in, customer_name is null. "
        "Never fill it with text from the vendor's own letterhead/address "
        "(e.g. the city/district printed in the vendor's business address) "
        "just because it appears near the blank customer block."
    ),
    "customer_contact": (
        "string or null â€” an individual contact person associated with the "
        "customer, typically marked 'Attn:', 'ATTN:', or 'c/o'. Leave null "
        "if no individual contact is named separately from the company."
    ),
    "customer_address": (
        "string or null — the customer's/billed-to ADDRESS, usually printed "
        "right below the 'Bill To'/'Sold To'/'Registered Name' block (often "
        "under an 'ADDRESS:' label). Null if that block is blank (walk-in "
        "customer with no billing info filled in)."
    ),
    "customer_tax_id": (
        "string or null — the CUSTOMER's TIN, usually labeled 'TIN' or 'TIN "
        "NO' right under the 'Bill To'/'Sold To' block. This is a different "
        "number from vendor_tax_id (the vendor's own 'VAT Reg. TIN' printed "
        "on their letterhead) — both commonly appear on the same invoice; "
        "don't confuse or merge them."
    ),
    "plate_number": (
        "string or null — the vehicle plate/plate number when the document "
        "contains one, especially parking tickets or vehicle-related receipts. "
        "Look for explicit labels such as 'Plate #', 'Plate No.', 'Plate Number', "
        "'Plate:', or 'Vehicle Plate'. Do not use ticket numbers, transaction "
        "numbers, OR/invoice numbers, MIN numbers, serial numbers, or other IDs. "
        "If no plate number is printed, return null."
    ),
    "line_items": [
        {"description": "string", "quantity": "number", "unit_price": "number", "amount": "number"}
    ],
    "subtotal": (
        "number â€” the Net Amount: the sum of line items before tax/VAT and "
        "discount is applied. Look for labels like 'Subtotal', 'Net Amount', "
        "or 'Net Total'. On Philippine BIR-formatted invoices this "
        "specifically corresponds to the 'VATABLE SALES' / 'Net of VAT' "
        "figure — the portion of sales that VAT was actually charged on. "
        "Do NOT include zero_rated_sales or vat_exempt_sales amounts here; "
        "those are separate sales categories with their own fields below."
    ),
    "tax_amount": (
        "number or null â€” the VAT (Value Added Tax) amount added on top of "
        "the subtotal/net amount. Also look for equivalent labels like "
        "'Tax', 'GST', or 'Sales Tax' if VAT isn't explicitly used."
    ),
    "tax_rate": "number or null â€” the VAT/tax rate as a percentage (e.g. 12 for 12% VAT)",
    "discount": "number or null",
    "zero_rated_sales": (
        "number or null — Philippine BIR invoices often break sales into "
        "THREE separate columns: VATABLE SALES (-> subtotal above), "
        "ZERO-RATED SALES, and VAT-EXEMPT SALES. This field is the "
        "'ZERO-RATED SALES' figure specifically (sales taxed at a 0% VAT "
        "rate, e.g. exports) — null if that column is blank/not printed, "
        "which is common; do not default it to 0 or to the vatable-sales "
        "figure."
    ),
    "vat_exempt_sales": (
        "number or null — the 'VAT-EXEMPT SALES' column (sales legally "
        "exempt from VAT altogether, e.g. senior-citizen/PWD-discounted "
        "purchases) — see zero_rated_sales above for the sibling column. "
        "Null if that column is blank/not printed on this invoice."
    ),
    "total_amount": (
        "number â€” the Total Amount Due: the final amount owed/payable for "
        "this transaction, i.e. the Net Amount plus VAT and any other "
        "applicable charges (also known as 'Gross Total'). Look for labels "
        "like TOTAL, AMOUNT DUE, AMOUNT PAYABLE, or GRAND TOTAL. Do NOT use "
        "CASH/TENDERED (money handed over by the customer) or CHANGE (money "
        "handed back) â€” those are payment mechanics, not the total."
    ),
    "currency": "3-letter ISO code",
    "payment_terms": (
        "string or null â€” e.g. 'Net 21', 'Net 30', 'Due on receipt'. "
        "IMPORTANT: a bare label 'Net:' followed by a small integer (e.g. "
        "'Net: 21') near the invoice/due dates or P.O. number is a payment "
        "term (days until due), NOT the subtotal/net amount â€” do not put "
        "that number in the 'subtotal' field."
    ),
}


CATEGORY_OPTIONS = [
    "Food", "Utilities", "Furnitures", "Office Supplies", "Transportation",
    "Insurance", "Medical & Health Supplies", "Others",
]
