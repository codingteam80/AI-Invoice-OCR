# AI Invoice OCR — User Manual (October 28, 2026)

## Install and configure

1. Use a compatible Python environment on the host PC, create and activate a virtual
   environment, and run `python -m pip install -r requirements.txt`.
2. Copy `.env.example` to `.env`. Configure your own API credentials; **do not share
   or upload `.env` with secrets**.
3. Default hosted API: `LLM_PROVIDER=api`, `LLM_BASE_URL=https://api.groq.com/openai/v1`,
   `LLM_MODEL=qwen/qwen3.8-27b`, and `LLM_API_KEY=...`. Existing `.env` values override
   defaults; change old OpenRouter settings if migrating.
4. Offline option: select **Ollama only** in the sidebar after installing Ollama and
   pulling `qwen2.5:7b`. Optional `qwen2.5vl:7b` vision verification runs only when
   extraction signals require it, when enabled and available. Without the vision model,
   OCR/text extraction can continue with verification warnings.
5. Launch `python app.py ui` or `start_server.bat`. Visit `http://localhost:8501`.
   For network-host setup, see `SETUP_SERVER.md`. For direct REST API use,
   `python app.py api`; note that its routes are not protected by Streamlit login.

## Uploading and scanning

- Upload only **one invoice per file**, JPG/PNG/TIFF/BMP or PDF. A multi-page file
  may contain several pages of the **same** invoice, but not separate invoices.
- Scan **one distinct invoice at a time**. In the Scan invoice tab, select the device,
  choose TWAIN/WIA/eSCL, select resolution (300 DPI is a useful starting point), and
  preview before processing. Combine pages only when they belong to one invoice.
- Blank, incomplete, and duplicate uploads have different notifications from model
  processing errors. Poor scans may produce incomplete results and verification flags.
- If the API fails, optional local Ollama fallback may be used when installed and
  enabled. The LLM does not fix information absent from the source image.

## Understanding invoice details

- History uses the **original filename as its title**; Invoice # appears once as
  a field. The approved fields are Vendor, Vendor Address, Vendor TIN, Customer,
  Customer Address, Customer TIN, Plate #, Invoice #, Invoice Date, Date Uploaded,
  Category, Vatable Sales, VAT-Exempt Sales, Zero-Rated Sales, VAT, Withholding Tax,
  Total Amount Due, Currency, Status, and Locked.
- Due Date, Tax Rate, Payment Terms, Customer Contact, Current Charges and Previous
  Balance are not displayed as standalone client fields or exported as columns.
  Current Charges / Previous Balance may appear as genuine extracted line items.
- The Qty column suppresses unnecessary trailing zeros. Evidence confidence scores
  are calculated and saved internally, but the UI highlights **only fields/cells
  that require verification** in amber.
- When a financial field is missing, the History UI may display `0.00` for
  convenience. This does **not** establish that the source invoice says zero.
- Review an invoice against its original document before locking it. Editing an
  extracted field invalidates its previous automated evidence score.

## Review triggers and correction

- Missing Vendor, Invoice #, Invoice Date, or Total Amount Due causes a review
  warning. A future invoice date is also flagged **even if OCR read it clearly**.
- Amount mismatches, low OCR evidence, nonmatching line items and incomplete PDF
  pages also generate warnings. `needs_review` is not a hard upload failure.
- Use the History **Edit this invoice** action to make corrections, then verify
  against the original source and lock only when satisfied. Existing saved
  records are not automatically rewritten when the code is upgraded.

## Currency rules

- Invoice currency uses ISO codes (e.g., `PHP` or `USD`). Philippine peso can
  appear as `PHP`, `₱` or `P` directly before a monetary amount.
- **Never add PHP and USD as if they were the same unit.** Dashboard shows
  per-currency totals and a currency selector for spending charts; Reports
  displays separate currency totals. CSV, Excel, and PDF exports contain
  one summary TOTAL row per currency. No exchange rate is applied.
- To compare currencies in PHP, obtain a dated, approved FX rate and perform
  an explicitly labeled conversion outside these native-currency totals.

## Pages, reports and logs

- **Home:** landing page after login. **Upload:** submit or scan invoices.
- **Dashboard:** invoice count, vendor count, review count, spend by currency,
  and vendor/category/month spending charts filtered to one currency.
- **History:** search, read original pages, verify, edit, lock/unlock or delete.
- **Reports:** filter by invoice date, upload date, vendor, amounts, category and
  supported AI Search requests; export CSV, Excel, PDF or preview print.
- **Log:** audit activity, search, export filtered records and print logs.
- **Users** (admin): create, deactivate and manage account access.

## Troubleshooting

| Problem | Suggested check |
|---|---|
| Groq API reports 401 | Use a valid Groq API key in `.env` and restart; check the base URL. |
| API 413 / 429 | Reduce image/request size or retry after limits; do not assume saved extraction is accurate. |
| Ollama model unavailable | Run `ollama list` and pull the required model; vision is optional. |
| Missing dates or totals | Review the original scan, including additional PDF pages. |
| Unexpected currency | Check the printed `USD`, `$`, `PHP`, `₱`, or monetary `P` marker and edit if necessary. |
| Old invoices still incorrect | Corrections apply to new extraction; manually edit or reprocess records after backup. |

See `README.md`, `SCANNER_PRINT_SETUP.md`, `SETUP_SERVER.md`, and
`docs/architecture.md` for more technical details.
