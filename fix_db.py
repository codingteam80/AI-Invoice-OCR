import sqlite3

conn = sqlite3.connect("data/invoices.db")
cur = conn.cursor()

expected_columns = {
    "invoice_number": "VARCHAR",
    "invoice_date": "DATE",
    "due_date": "DATE",
    "vendor_id": "INTEGER",
    "vendor_name": "VARCHAR",
    "customer_name": "VARCHAR",
    "subtotal": "FLOAT",
    "tax_amount": "FLOAT",
    "tax_rate": "FLOAT",
    "discount": "FLOAT",
    "total_amount": "FLOAT",
    "currency": "VARCHAR",
    "payment_terms": "VARCHAR",
    "category": "VARCHAR",
    "source_file": "VARCHAR",
    "original_filename": "VARCHAR",
    "ocr_engine_used": "VARCHAR",
    "confidence_score": "FLOAT",
    "status": "VARCHAR",
    "raw_text": "TEXT",
    "locked": "BOOLEAN",
    "created_at": "DATETIME",
}

cur.execute("PRAGMA table_info(invoices)")
existing_cols = {row[1] for row in cur.fetchall()}

added = []
for col_name, col_type in expected_columns.items():
    if col_name not in existing_cols:
        cur.execute(f"ALTER TABLE invoices ADD COLUMN {col_name} {col_type}")
        added.append(col_name)

conn.commit()
conn.close()

print("Added columns:", added if added else "none — already in sync")
