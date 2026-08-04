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
- Streamlit UI: Upload, Dashboard (spend analytics), History (search), Reports
  (Excel/CSV/PDF export)
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
