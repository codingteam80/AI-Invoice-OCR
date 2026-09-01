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
            ("vendor_address", "TEXT"), ("vendor_tax_id", "TEXT"),
            ("customer_contact", "TEXT"), ("customer_address", "TEXT"), ("customer_tax_id", "TEXT"),
            ("zero_rated_sales", "REAL"), ("vat_exempt_sales", "REAL"),
        ],
    }
    with engine.connect() as conn:
        for table, columns in new_columns.items():
            existing = {row[1] for row in conn.exec_driver_sql(f"PRAGMA table_info({table})").fetchall()}
            for col_name, col_type in columns:
                if col_name not in existing:
                    logger.info(f"Migrating: adding column {table}.{col_name}")
                    conn.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {col_name} {col_type}")
        conn.commit()


def get_session():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
