> October 15 repair (v6): read [READ_FIRST_REPAIR_V6.md](READ_FIRST_REPAIR_V6.md) before upgrading. It supersedes older deadline instructions.

# AI-Invoice-OCR

AI-assisted invoice extraction for printed and handwritten documents: image preprocessing,
PaddleOCR (plus TrOCR for handwriting), structured LLM extraction, evidence checks, validation,
and SQLite storage. Choose Groq API or local Ollama. **API mode transmits invoice content
and images to a cloud provider**; use local-only mode when required by data-handling rules.

## Features

- Multi-engine OCR with automatic fallback and image preprocessing (deskew,
  denoise, adaptive threshold)
- Schema-constrained LLM extraction with automatic self-correction retries
- Business-rule validation (totals reconciliation, required fields, currency
  format) and a per-invoice confidence score
- SQLite storage via SQLAlchemy, with vendor deduplication
- Streamlit UI: Upload, Dashboard (spend analytics), History, Reports
  (Excel/CSV/PDF export), and Activity Log
- Natural-language search on History, Reports, and Log; the configured
  text model interprets search filters without executing raw model-generated SQL
- Optional FastAPI REST API for integrations
- Cloud API or local Ollama processing; invoice files stay on your PC in local mode


## Default API: Groq Qwen3.8 27B

Use the OpenAI-compatible Groq endpoint in your private **.env** (never commit API keys):

```
LLM_PROVIDER=api
LLM_API_KEY=your-groq-api-key
LLM_BASE_URL=https://api.groq.com/openai/v1
LLM_MODEL=qwen/qwen3.8-27b
DEFAULT_CURRENCY=PHP
```

These model/URL defaults are also in `config/settings.py` and `.env.example`.
Existing `.env` values take precedence: change an old OpenRouter URL/model in `.env` or the
app will continue using them. Restart after editing. To use **Ollama only**, select Ollama
in the sidebar or set `LLM_PROVIDER=ollama`. A missing/invalid Groq key cannot be fixed by
model defaults alone.
The retry/fallback notes below apply to every hosted provider.

## Using OpenRouter instead of local Ollama

1. Create a key at https://openrouter.ai/keys and put it in your **.env** (never commit it):

   ```
   LLM_PROVIDER=openrouter
   OPENROUTER_API_KEY=sk-or-...
   OPENROUTER_MODEL=openrouter/free
   ```

2. Restart the app. Text extraction, AI Search and (if enabled) vision verification all go through OpenRouter.
   Set `LLM_PROVIDER=ollama` to switch back to the local models.

With `LLM_PROVIDER=ollama` the local-model path is exactly the original: same requests, same options, same timeouts.

Retries: transient OpenRouter failures (HTTP 408/425/429/5xx, connection errors, an empty answer, or an answer that is
not valid JSON) are retried up to `OPENROUTER_MAX_RETRIES` times with exponential backoff (`Retry-After` is honoured),
within `OPENROUTER_RETRY_BUDGET_SECONDS`. Timeouts get `OPENROUTER_TIMEOUT_RETRIES` extra tries. Permanent errors
(400/401/402/403/404) are not retried. Each retry is recorded in the diagnostic trace (`usage.retries`).

Automatic fallback: with `LLM_FALLBACK_TO_OLLAMA=true` (default), a failed OpenRouter request (invalid key, no credits,
rate limit, timeout, 5xx) is repeated on local Ollama, and OpenRouter is skipped for `OPENROUTER_COOLDOWN_SECONDS`
(600s for 401/402/403) or `OPENROUTER_SHORT_COOLDOWN_SECONDS` (60s otherwise). Ollama must be running with the
model pulled for the fallback to work. Failover details are stored in the diagnostic trace (`fallback_from`).

Notes: `openrouter/free` picks a random free model per request, so results can vary between calls and free models
are rate limited. Invoice text/images are sent to third-party providers - do not use free models for sensitive documents
unless you have checked their data policies. Pin a specific model id in `OPENROUTER_MODEL` for repeatable results.

## Quick start

```bash
pip install -r requirements.txt
# Windows: copy .env.example .env (then set your API key or choose Ollama)
cp .env.example .env
# For local-only/fallback extraction: ollama pull qwen2.5:7b
# Optional local vision: ollama pull qwen2.5vl:7b
python app.py ui
```

Use **one invoice per uploaded file** (a genuine multi-page invoice is supported, multiple
separate invoices in a file are rejected). Scan one invoice at a time. PHP amounts may be
marked `PHP`, `₱`, or `P` in a monetary context; USD amounts are stored and reported
separately from PHP. **Never add PHP and USD totals without an explicit exchange rate**.

Missing Vendor, Invoice #, Invoice Date, Total Amount Due, and future Invoice Dates are
flagged for review. Highlighted values need source verification. Source confidence remains
internal. The system does not automatically repair already-saved database records; reprocess
or edit them (while keeping a backup) to benefit from new extraction rules.

See `docs/user_manual.md` for full setup instructions, `docs/architecture.md`
for how the pipeline fits together, and `docs/api.md` for the REST API.

## Tech stack

| Category | Tool |
|---|---|
| Language | Python 3.11+ |
| UI | Streamlit |
| OCR | PaddleOCR (primary), Tesseract, EasyOCR |
| AI | Ollama (Qwen 2.5 / Llama 3) |
| Image processing | OpenCV, Pillow |
| PDF processing | PyMuPDF |
| Database | SQLite + SQLAlchemy |
| Validation | Pydantic |
| Data / export | pandas, openpyxl, reportlab |
| API | FastAPI + uvicorn |
| Testing | pytest |

## Project structure

See `docs/architecture.md` for a full breakdown of each folder's role.

## Running tests

```bash
pytest tests/ -v
```


## Login & user accounts (offline)

The UI requires a sign-in. Accounts live in the local `users` table (passwords stored as salted PBKDF2-SHA256 hashes — no external service).
Every logged action (upload, edit, lock, delete, export…) is recorded in the **Log** page under the signed-in username.

* **First run:** an admin account is created automatically — username `admin`, password `admin1234`
  (override with `DEFAULT_ADMIN_USERNAME` / `DEFAULT_ADMIN_PASSWORD` in `.env` *before* first start). You are forced to choose a new password at first login.
* **Admins** get a **Users** page to create accounts, deactivate/reactivate, change roles and reset passwords. New/reset accounts must set their own password at next login.
* Login is restored from a local browser cookie after refresh or reconnect while the same Streamlit server process is running. Stopping/restarting Streamlit or shutting down the PC requires a new sign-in. Use **Sign out** to end it. Clearing browser cookies, disabling an account, or resetting its password also invalidates access.
* Each new sign-in opens **Home**. Signing out clears temporary Upload results and page selections; saved invoices remain in History.
* The REST API (`api/`) is not covered by this login; changes made through it are logged under the PC's OS username.

**Multi-PC use:** see `SETUP_SERVER.md` (one Host PC runs `start_server.bat`; other PCs just open its address in a browser).

## Scanner, log downloads, and printing

Upload now supports direct flatbed scanning through free NAPS2 on Windows. Log downloads all filtered/searched records as Excel, CSV, or PDF and supports printing. Reports prints the same A3 PDF layout used by Generate Export, with browser printer selection. See [SCANNER_PRINT_SETUP.md](SCANNER_PRINT_SETUP.md) for installation and use.

## CPU speed and verification reliability

See [CPU_SPEED_UPDATE.md](CPU_SPEED_UPDATE.md) for the batch vision-timeout guard, earlier cleanup before text correction, review warnings, and new timing fields. Both improvements are enabled by default without changing your existing .env.

## October 26, 2026 document-evidence QA revision

This build is based on `AI-Invoice-OCR-API-Ollama-Dan-QA-Fixed-v6.zip`. It adds source-evidence checks for utility/telecom statement amounts, addresses, credits and line items, and highlights genuinely unverified date/identifier/tax facts rather than hardcoding one invoice's expected answers. See `docs/QA_EVIDENCE_REVIEW_2026-10-26.md` for individual invoice findings and limitations. The optional low-resolution image utility is `tools/enhance_low_resolution_invoice.py`.

Use **one invoice per file**; an invoice may itself have multiple pages. Enhanced images cannot regenerate text lost in the original capture, and missing VATable Sales or VAT must not be interpreted as a confirmed zero. Previously saved database records are not rewritten by this upgrade.

## Consolidated October 26 edition — approved UI and conditional vision

This edition includes the source-based October 26 QA corrections **plus** the October 23 conditional vision, evidence-based review highlights and client-approved History/export interface. Refer to [`CONSOLIDATION_NOTES_2026-10-26.md`](CONSOLIDATION_NOTES_2026-10-26.md) for the exact behavior and limitations.

- With `VISION_VERIFICATION_ENABLED=true`, `qwen2.5vl:7b` is used for difficult invoices, not automatically for every clean printed invoice. The app checks availability before uploads and skips optional local vision if the model is missing; text OCR/LLM processing remains available.
- History shows the uploaded filename as heading, compact amber verification flags, and the approved invoice/financial fields only. Overall confidence stays internal; evidence is stored for fields and individual line-item cells.
- Upload **one invoice per file**. Multiple pages of the **same** invoice are supported when correctly grouped. Multiple separate invoices in one file are rejected without saving partial results.
- Export columns exclude internal fields such as Due Date, Tax Rate, Payment Terms, Customer Contact, Current Charges Total and Previous Balance.
- A missing amount may appear as `0.00` in the History display, but remains missing in the database; never treat an inferred zero as a confirmed amount.

## October 28, 2026 QA update

- Corrected Bill-to ownership for invoices where metadata table headings appear before the
  customer name in OCR reading order (for example, NOTARIZE INV-29).
- Highlight future invoice dates even when the OCR confidence is high; missing required
  fields continue to trigger `needs_review`.
- Isolate monetary summaries by **currency** on Dashboard, Reports, exports, and charts.
  No automatic exchange-rate conversion is performed; mixed-currency exports have one total
  row per currency.
- Updated default API endpoint/model to Groq (`qwen/qwen3.8-27b`); original
  provider overrides still work.
- See `docs/QA_OCT28_CHANGELOG.md` for tests and limitations.
