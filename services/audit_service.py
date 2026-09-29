"""Persistent user activity/audit log for UI actions."""
from datetime import datetime, timezone, tzinfo
import getpass
import os

from database.database import SessionLocal
from database.models import AuditLogORM


def current_pc_user() -> str:
    """Best-effort OS account name for the PC/session running Streamlit."""
    return (os.environ.get("USERNAME") or os.environ.get("USER") or getpass.getuser() or "Unknown").strip()


def current_app_username() -> str:
    """Logged-in app user for the current Streamlit session.

    Falls back to the PC's OS account when there is no login session (REST API,
    CLI, background scripts), so audit rows are never left without a name.
    """
    try:
        from streamlit.runtime.scriptrunner import get_script_run_ctx
        if get_script_run_ctx() is not None:
            import streamlit as st
            from config.constants import AUTH_SESSION_KEY
            user = st.session_state.get(AUTH_SESSION_KEY)
            if user and user.get("username"):
                return str(user["username"])
    except Exception:
        pass
    return current_pc_user()


def audit_utc_to_local(value: datetime | None, target_tz: tzinfo | None = None) -> datetime | None:
    """Convert the database's UTC audit timestamp to the PC's local time.

    Historical AuditLogORM.created_at values are naive UTC because SQLAlchemy
    stored ``datetime.utcnow()``. Treat those values explicitly as UTC before
    converting them. With ``target_tz=None``, ``astimezone()`` uses the local
    timezone configured on the PC running Streamlit.
    """
    if value is None:
        return None
    utc_value = value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
    return utc_value.astimezone(target_tz) if target_tz is not None else utc_value.astimezone()


def log_action(action: str, entity_type: str | None = None, entity_id: int | None = None, details: str | None = None, username: str | None = None) -> None:
    session = SessionLocal()
    try:
        session.add(AuditLogORM(username=username or current_app_username(), action=action, entity_type=entity_type, entity_id=entity_id, details=details))
        session.commit()
    finally:
        session.close()


def list_logs(limit: int | None = 2000) -> list[dict]:
    session = SessionLocal()
    try:
        query = session.query(AuditLogORM).order_by(AuditLogORM.created_at.desc(), AuditLogORM.id.desc())
        if limit is not None:
            query = query.limit(limit)
        rows = query.all()
        return [
            {
                "id": r.id,
                "date_time": audit_utc_to_local(r.created_at),
                "user": r.username,
                "action": r.action,
                "entity_type": r.entity_type,
                "entity_id": r.entity_id,
                "details": r.details,
            }
            for r in rows
        ]
    finally:
        session.close()
