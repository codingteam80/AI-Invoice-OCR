"""Replace the legacy global invoice-number index without losing invoice rows."""
import re
from utils.data_quality import duplicate_key


def migrate_duplicate_identity(engine):
    if engine.dialect.name != "sqlite":
        return
    raw = engine.raw_connection()
    try:
        cursor = raw.cursor()
        foreign_keys = cursor.execute("PRAGMA foreign_keys").fetchone()[0]
        raw.commit()
        cursor.execute("PRAGMA foreign_keys=OFF")
        cursor.execute("BEGIN IMMEDIATE")
        indexes = cursor.execute("PRAGMA index_list(invoices)").fetchall()
        rebuild = False
        for index in indexes:
            name = index[1]
            columns = [r[2] for r in cursor.execute('PRAGMA index_info("' + name.replace('"', '""') + '")')]
            if index[2] and columns == ["invoice_number"]:
                if index[3] == "u":
                    rebuild = True
                else:
                    cursor.execute('DROP INDEX "' + name.replace('"', '""') + '"')
        if rebuild:
            schema = cursor.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='invoices'").fetchone()[0]
            # Preserve all columns, constraints and user-created indexes/triggers.
            schema = re.sub(r',\s*UNIQUE\s*\(\s*invoice_number\s*\)', '', schema, flags=re.I)
            schema = re.sub(r'(\binvoice_number\s+\w+(?:\([^)]*\))?\s*(?:NOT NULL\s*)?)UNIQUE', r'\1', schema, flags=re.I)
            schema = re.sub(r'CREATE TABLE\s+["`\[]?invoices["`\]]?', 'CREATE TABLE invoices_qa_new', schema, count=1, flags=re.I)
            objects = cursor.execute("SELECT sql FROM sqlite_master WHERE tbl_name='invoices' AND type IN ('index','trigger') AND sql IS NOT NULL").fetchall()
            columns = ', '.join('"' + row[1] + '"' for row in cursor.execute('PRAGMA table_info(invoices)'))
            cursor.execute(schema)
            cursor.execute(f'INSERT INTO invoices_qa_new ({columns}) SELECT {columns} FROM invoices')
            cursor.execute('DROP TABLE invoices')
            cursor.execute('ALTER TABLE invoices_qa_new RENAME TO invoices')
            for (sql,) in objects:
                cursor.execute(sql)
        cursor.execute('CREATE INDEX IF NOT EXISTS ix_invoices_invoice_number ON invoices(invoice_number)')
        rows = cursor.execute('SELECT id, vendor_name, invoice_number FROM invoices ORDER BY id').fetchall()
        # Clear old keys before recalculation: punctuation-spacing aliases can
        # now collide, and historical duplicates must be retained for review.
        cursor.execute("UPDATE invoices SET duplicate_key=NULL")
        seen = set()
        for row_id, vendor, number in rows:
            key = duplicate_key(vendor, number)
            if key in seen:
                # Preserve historical duplicates, but make their review requirement visible.
                key = None
                cursor.execute("UPDATE invoices SET status='needs_review' WHERE id=?", (row_id,))
            if key:
                seen.add(key)
            cursor.execute('UPDATE invoices SET duplicate_key=? WHERE id=?', (key, row_id))
        cursor.execute('CREATE UNIQUE INDEX IF NOT EXISTS ux_invoices_duplicate_key ON invoices(duplicate_key)')
        if cursor.execute('PRAGMA foreign_key_check').fetchall():
            raise RuntimeError('Invoice identity migration failed foreign-key validation')
        raw.commit()
    except Exception:
        raw.rollback()
        raise
    finally:
        try:
            raw.cursor().execute(f'PRAGMA foreign_keys={foreign_keys}')
        finally:
            raw.close()
