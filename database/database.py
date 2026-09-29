"""SQLAlchemy engine, session factory, and init helper."""
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base
from config.settings import settings
from config.logging import get_logger

logger = get_logger("database")

engine = create_engine(
    settings.DATABASE_URL,
    connect_args={"check_same_thread": False} if settings.DATABASE_URL.startswith("sqlite") else {},
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def init_db():
    from database import models  # noqa: F401 — ensures models are registered
    Base.metadata.create_all(bind=engine)
    _ensure_new_columns()


def _ensure_new_columns():
    """Lightweight migration for columns added after a DB already exists.

    Base.metadata.create_all() only creates missing TABLES — it silently
    does nothing to a table that already exists, even if the ORM model now
    defines extra columns. Without this, an already-deployed sqlite DB
    (e.g. one carried over from before vision_notes was added) would throw
    "no such column" the first time it's queried. There's no Alembic in
    this project, so this is a deliberately minimal stand-in: add any
    columns that are missing, one ALTER TABLE at a time, and do nothing if
    they're already there.
    """
    if not settings.DATABASE_URL.startswith("sqlite"):
        return  # Non-sqlite deployments should use a real migration tool.

    new_columns = {
        "invoices": [
            ("vision_notes", "TEXT"), ("enhanced_image_path", "TEXT"),
            ("date_uploaded", "DATE"),
            ("vendor_address", "TEXT"), ("vendor_tax_id", "TEXT"),
            ("customer_contact", "TEXT"), ("customer_address", "TEXT"), ("customer_tax_id", "TEXT"),
            ("plate_number", "TEXT"),
            ("zero_rated_sales", "REAL"), ("vat_exempt_sales", "REAL"),
            ("current_charges_total", "REAL"), ("previous_balance", "REAL"),
            ("withholding_tax", "REAL"),
            # raw_text has been on the Invoice/InvoiceORM models since
            # before this migration list existed, so it was never added
            # here — but a sqlite file created before raw_text was on the
            # model (very early deployments) would still be missing the
            # column, and every subsequent save would silently insert NULL
            # into it instead of the actual OCR text. Listed here as a
            # safety net so a legacy DB gets the column added on next app
            # startup, same as the others above.
            ("raw_text", "TEXT"),
        ],
    }
    with engine.connect() as conn:
        for table, columns in new_columns.items():
            existing = {row[1] for row in conn.exec_driver_sql(f"PRAGMA table_info({table})").fetchall()}
            for col_name, col_type in columns:
                if col_name not in existing:
                    logger.info(f"Migrating: adding column {table}.{col_name}")
                    conn.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {col_name} {col_type}")
        # Backfill legacy rows once: their created_at timestamp is the historical upload/save time.
        if "date_uploaded" in {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(invoices)").fetchall()}:
            conn.exec_driver_sql("UPDATE invoices SET date_uploaded = DATE(created_at) WHERE date_uploaded IS NULL AND created_at IS NOT NULL")
        # v1.64 display normalization: vendor/customer names are stored in ALL CAPS.
        conn.exec_driver_sql("UPDATE invoices SET vendor_name = UPPER(TRIM(vendor_name)) WHERE vendor_name IS NOT NULL")
        conn.exec_driver_sql("UPDATE invoices SET customer_name = UPPER(TRIM(customer_name)) WHERE customer_name IS NOT NULL")
        conn.exec_driver_sql("UPDATE vendors SET name = UPPER(TRIM(name)) WHERE name IS NOT NULL")
        _repair_v167_zero_to_null_edit_bug(conn)
        conn.commit()


def _repair_v167_zero_to_null_edit_bug(conn):
    """Restore explicit 0.00 amounts that v1.67 could accidentally save as NULL.

    The old History save payload used ``value or None`` for optional financial
    fields. Therefore an untouched, explicit 0.00 became NULL simply by
    clicking Save. Repair only invoices whose audit trail explicitly proves
    that exact transition, so genuine blank values are left untouched.
    """
    tables = {row[0] for row in conn.exec_driver_sql(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()}
    if "audit_logs" not in tables or "invoices" not in tables:
        return

    repairs = [
        ("Discount (internal): 0.00 → —", "discount"),
        ("Withholding Tax: 0.00 → —", "withholding_tax"),
        ("Zero-Rated Sales: 0.00 → —", "zero_rated_sales"),
        ("VAT-Exempt Sales: 0.00 → —", "vat_exempt_sales"),
    ]
    total_repaired = 0
    for marker, column in repairs:
        rows = conn.exec_driver_sql(
            "SELECT DISTINCT entity_id FROM audit_logs "
            "WHERE action = 'EDIT' AND entity_type = 'invoice' "
            "AND details LIKE ? AND entity_id IS NOT NULL",
            (f"%{marker}%",),
        ).fetchall()
        for (invoice_id,) in rows:
            result = conn.exec_driver_sql(
                f"UPDATE invoices SET {column} = 0.0 WHERE id = ? AND {column} IS NULL",
                (invoice_id,),
            )
            total_repaired += max(result.rowcount or 0, 0)
    if total_repaired:
        logger.info(f"Repaired {total_repaired} v1.67 explicit-zero financial value(s) saved as NULL")


def get_session():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
