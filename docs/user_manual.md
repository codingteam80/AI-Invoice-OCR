# User Manual

## 1. Setup

```bash
git clone <your-repo-url> AI-Invoice-OCR
cd AI-Invoice-OCR
python -m venv venv && source venv/bin/activate   # Windows: venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
```

Install and start [Ollama](https://ollama.com), then pull a model:
```bash
ollama pull qwen2.5:7b
```

(Optional) install Tesseract for the fallback OCR engine — see your OS package
manager (`apt install tesseract-ocr`, `brew install tesseract`, etc).

## 2. Run the app

Web UI (recommended for most users):
```bash
python app.py ui
```
Opens Streamlit at `http://localhost:8501`.

REST API (for integrations):
```bash
python app.py api
```
Runs at `http://localhost:8000` — see `docs/api.md`.

Command line (single file):
```bash
python app.py process data/samples/invoice1.png
```

## 3. Using the UI

- **Upload** — drag & drop invoice images/PDFs, pick an OCR engine, click
  "Process Invoices". Each result shows vendor, total, and a confidence score.
- **Dashboard** — spend by vendor, spend by month, and a status breakdown chart.
- **History** — search and browse every processed invoice; click an ID to see
  full details including line items.
- **Reports** — export filtered invoices to Excel, CSV, or PDF.

## 4. Understanding confidence & review status

Each invoice gets a confidence score (0–100%). Anything below the configured
threshold (`CONFIDENCE_THRESHOLD` in `.env`, default 75%) — or that fails a
business-rule check like totals not adding up — is marked **needs_review**.
Review these manually before relying on the extracted numbers.

## 5. Troubleshooting

| Symptom | Likely cause |
|---|---|
| "Ollama request failed" | Ollama isn't running — start it with `ollama serve` |
| All invoices marked `needs_review` | Model/OCR combo struggling with your invoice layout — try a different OCR engine, or a larger LLM |
| Slow processing | Larger LLM models are slower on CPU; try a smaller Ollama model, or enable GPU |
