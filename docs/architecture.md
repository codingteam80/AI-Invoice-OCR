# Architecture

## Pipeline overview

```
Upload (Streamlit / API)
    │
    ▼
services/upload_service.py   — validates & saves file to data/uploads/
    │
    ▼
parser/invoice_parser.py     — orchestrates the pipeline below
    │
    ├─▶ ocr/ocr_engine.py     — PaddleOCR (primary) → Tesseract → EasyOCR fallback
    │        └─ ocr/preprocessing.py (deskew, denoise, threshold)
    │
    ├─▶ ai/extractor.py       — calls local Ollama model (Qwen2.5 / Llama3)
    │        ├─ ai/prompt_builder.py   (schema-constrained prompt)
    │        └─ ai/validator.py        (business-rule checks, triggers retry)
    │
    ├─▶ ai/post_processing.py — normalizes dates, currency, numbers
    ├─▶ ai/confidence.py      — combines OCR + validation into a confidence score
    │
    ▼
models/invoice.py            — Pydantic Invoice model (typed, validated)
    │
    ▼
database/repository.py       — persists to SQLite via SQLAlchemy
    │
    ▼
services/invoice_service.py  — archives JSON, moves file to processed/
```

## Layers

- **config/** — environment-driven settings, logging, constants/schema.
- **ocr/** — pluggable OCR backends behind a single `extract_from_file()` interface.
- **ai/** — LLM prompt construction, extraction call, validation, confidence scoring.
- **parser/** — normalization helpers (currency, dates, tax, table heuristics) and the
  top-level `parse_invoice()` orchestrator.
- **models/** — Pydantic schemas (API/business layer), separate from...
- **database/** — SQLAlchemy ORM models + repository pattern for persistence.
- **services/** — use-case layer that ties parser + database + exports together;
  this is what both the Streamlit UI and the FastAPI routes call into.
- **exports/** — format-specific writers (xlsx/csv/pdf).
- **api/** — thin FastAPI routers, useful for integrating with other systems.
- **ui/** — Streamlit multi-page app (Upload, Dashboard, History, Reports).

## Confidence & review workflow

Every processed invoice gets an `overall_confidence` score (0–1) combining:
1. Average OCR confidence across recognized text lines.
2. A penalty for each business-rule validation issue (missing fields, totals
   mismatch, invalid currency, etc).

If confidence falls below `CONFIDENCE_THRESHOLD` (default 0.75) or any
validation issue remains after retries, the invoice status is set to
`needs_review` instead of `processed`, and it's surfaced in the UI/dashboard
for manual correction.

## Why local LLM (Ollama)?

Invoices often contain sensitive financial data. Using a local model (Qwen 2.5
or Llama 3 via Ollama) keeps all document content on-premises — no data leaves
the machine running the pipeline.
