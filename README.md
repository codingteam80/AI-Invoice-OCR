# AI-Invoice-OCR

Local, privacy-friendly invoice extraction: OCR (PaddleOCR/Tesseract/EasyOCR)
+ a local LLM (Qwen 2.5 / Llama 3 via Ollama) turn scanned invoices and PDFs
into structured, validated, searchable data — no cloud calls required.

## Features

- Multi-engine OCR with automatic fallback and image preprocessing (deskew,
  denoise, adaptive threshold)
- Schema-constrained LLM extraction with automatic self-correction retries
- Business-rule validation (totals reconciliation, required fields, currency
  format) and a per-invoice confidence score
- SQLite storage via SQLAlchemy, with vendor deduplication
- Streamlit UI: Upload, Dashboard (spend analytics), History, Reports
  (Excel/CSV/PDF export), and Activity Log
- Local Qwen2.5:7b natural-language search on History, Reports, and Log; the
  model translates plain-English requests into validated filters and never
  executes raw SQL
- Optional FastAPI REST API for integrations
- Fully local — invoice data never leaves your machine

## Quick start

```bash
pip install -r requirements.txt
cp .env.example .env
ollama pull qwen2.5:7b
python app.py ui
```

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
