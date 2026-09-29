"""Remember local logins across Streamlit reconnects; revoke on explicit logout.

Browser refresh preserves access; stopping/restarting the server invalidates it. Resetting its password or
turning it off also invalidates its sessions, including already-open tabs.
"""
import hashlib
import hmac
import secrets
from datetime import datetime

from database.database import SessionLocal
from database.models import BrowserLoginSessionORM, UserORM
from services.user_service import UserError, _to_dict

COOKIE_NAME = "ai_invoice_ocr_login_v2"
# Process-local only: survives browser refresh, never survives a server restart.
# No shutdown callback is needed, so forced termination/power loss is covered.
_PROCESS_LOGIN_KEY = secrets.token_bytes(32)


def _token_digest(token: str) -> str:
    return hmac.new(_PROCESS_LOGIN_KEY, token.encode("utf-8"), hashlib.sha256).hexdigest()



def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def create_browser_session(user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    with SessionLocal() as db:
        user = db.get(UserORM, user_id)
        if user is None or not user.is_active:
            raise UserError("Account is unavailable.")
        db.add(BrowserLoginSessionORM(user_id=user.id, token_hash=_token_digest(token),
                                     password_fingerprint=_digest(user.password_hash)))
        db.commit()
    return token


def browser_session_user(token: str | None) -> dict | None:
    if not token or not isinstance(token, str) or len(token) > 128:
        return None
    with SessionLocal() as db:
        row = db.query(BrowserLoginSessionORM).filter_by(token_hash=_token_digest(token)).first()
        if row is None or row.revoked_at is not None:
            return None
        user = db.get(UserORM, row.user_id)
        if (user is None or not user.is_active or
                row.password_fingerprint != _digest(user.password_hash)):
            return None
        return _to_dict(user)


def revoke_browser_session(token: str | None) -> None:
    if not token:
        return
    with SessionLocal() as db:
        row = db.query(BrowserLoginSessionORM).filter_by(token_hash=_token_digest(token)).first()
        if row is not None and row.revoked_at is None:
            row.revoked_at = datetime.utcnow()
            db.commit()
