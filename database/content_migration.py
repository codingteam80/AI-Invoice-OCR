"""Backfill exact source-content identity without deleting historical rows."""
import json
from utils.data_quality import document_content_key


def migrate_content_identity(engine):
    if engine.dialect.name != 'sqlite':
        return
    with engine.begin() as conn:
        columns = {r[1] for r in conn.exec_driver_sql('PRAGMA table_info(invoices)')}
        if 'content_key' not in columns:
            conn.exec_driver_sql('ALTER TABLE invoices ADD COLUMN content_key TEXT')
        rows = conn.exec_driver_sql('SELECT id, raw_text, validation_issues FROM invoices ORDER BY id').fetchall()
        conn.exec_driver_sql('UPDATE invoices SET content_key=NULL')
        seen = {}
        for row_id, raw_text, issues_json in rows:
            key = document_content_key(raw_text)
            if key and key in seen:
                try:
                    issues = json.loads(issues_json or '[]')
                except (ValueError, TypeError):
                    issues = []
                if not isinstance(issues, list):
                    issues = []
                note = f'Duplicate source content matches invoice #{seen[key]}; review historical duplicate.'
                issues = list(dict.fromkeys(issues + [note]))
                conn.exec_driver_sql("UPDATE invoices SET status='needs_review', validation_issues=? WHERE id=?", (json.dumps(issues), row_id))
            elif key:
                seen[key] = row_id
                conn.exec_driver_sql('UPDATE invoices SET content_key=? WHERE id=?', (key, row_id))
        conn.exec_driver_sql('CREATE UNIQUE INDEX IF NOT EXISTS ux_invoices_content_key ON invoices(content_key)')
