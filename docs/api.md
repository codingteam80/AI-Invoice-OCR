# API Reference

Base URL: `http://localhost:8000`

Start with: `python app.py api`

## Health

`GET /health` → `{"status": "ok"}`

## Upload

`POST /api/upload/`
Multipart form-data with a `file` field (image or PDF).

Response:
```json
{
  "success": true,
  "invoice_id": 1,
  "invoice_number": "INV-2024-001",
  "vendor_name": "Acme Corp",
  "total_amount": 1234.56,
  "currency": "USD",
  "confidence_score": 0.91,
  "status": "processed",
  "processed_file": "data/processed/....png"
}
```

## Invoices

- `GET /api/invoices/?limit=100&status=processed` — list invoices
- `GET /api/invoices/{id}` — get one invoice with line items
- `GET /api/invoices/search?q=acme` — search by invoice #/vendor/customer
- `GET /api/invoices/stats` — dashboard aggregate stats
- `GET /api/invoices/export/{fmt}` — export all (or filtered) invoices;
  `fmt` is one of `xlsx`, `csv`, `pdf`

## Error format

Non-2xx responses return:
```json
{"detail": "human readable message"}
```
