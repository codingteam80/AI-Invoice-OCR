"""Local (offline) user accounts: password hashing, login, and admin management.

No external service or extra dependency — passwords are hashed with
PBKDF2-HMAC-SHA256 from the standard library, each with its own random salt.
"""
import hashlib
import hmac
import re
import secrets
from datetime import datetime

from config.constants import MIN_PASSWORD_LENGTH, USER_ROLES
from config.logging import get_logger
from config.settings import settings
from database.database import SessionLocal, init_db
from database.repository import UserRepository
from services.audit_service import log_action

logger = get_logger("user_service")

_ALGO = "pbkdf2_sha256"
_ITERATIONS = 260_000
_USERNAME_RE = re.compile(r"^[a-z0-9._-]{3,32}$")


class UserError(Exception):
    """A user-facing validation/permission problem (message is safe to display)."""


# ---------------------------------------------------------------- hashing
def hash_password(password: str, iterations: int = _ITERATIONS) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), iterations)
    return f"{_ALGO}${iterations}${salt}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, iterations, salt, expected = stored.split("$")
        if algo != _ALGO:
            return False
        digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), int(iterations))
        return hmac.compare_digest(digest.hex(), expected)
    except (ValueError, TypeError):
        return False


# ------------------------------------------------------------ validation
def _clean_username(username: str) -> str:
    name = (username or "").strip().lower()
    if not _USERNAME_RE.match(name):
        raise UserError("Username must be 3–32 characters: letters, numbers, dot, dash or underscore.")
    return name


def _check_password(password: str) -> None:
    if len(password or "") < MIN_PASSWORD_LENGTH:
        raise UserError(f"Password must be at least {MIN_PASSWORD_LENGTH} characters.")


def _to_dict(u) -> dict:
    return {
        "id": u.id, "username": u.username, "role": u.role, "is_active": bool(u.is_active),
        "must_change_password": bool(u.must_change_password),
        "created_at": u.created_at, "last_login_at": u.last_login_at,
    }


# --------------------------------------------------------------- bootstrap
def ensure_default_admin() -> bool:
    """Create the first admin if (and only if) there are no users yet."""
    session = SessionLocal()
    try:
        repo = UserRepository(session)
        if repo.count() > 0:
            return False
        repo.create(
            settings.DEFAULT_ADMIN_USERNAME, hash_password(settings.DEFAULT_ADMIN_PASSWORD),
            role="admin", must_change_password=True,
        )
        session.commit()
        logger.info("Created default admin account '%s' (must change password on first login)", settings.DEFAULT_ADMIN_USERNAME)
        return True
    finally:
        session.close()


def bootstrap_auth() -> None:
    """Make sure tables exist and a first admin is available. Safe to call repeatedly."""
    init_db()
    ensure_default_admin()


# ------------------------------------------------------------------- login
def authenticate(username: str, password: str) -> dict | None:
    """Return the user dict on success, None for wrong credentials or a disabled account."""
    session = SessionLocal()
    try:
        repo = UserRepository(session)
        user = repo.get_by_username(username)
        if user is None:
            # Burn comparable time so unknown usernames aren't distinguishable by speed.
            verify_password(password or "", hash_password("dummy-password"))
            return None
        if not verify_password(password or "", user.password_hash) or not user.is_active:
            return None
        user.last_login_at = datetime.utcnow()
        session.commit()
        return _to_dict(user)
    finally:
        session.close()


# -------------------------------------------------------------- management
def list_users() -> list[dict]:
    session = SessionLocal()
    try:
        return [_to_dict(u) for u in UserRepository(session).list_all()]
    finally:
        session.close()


def create_user(username: str, password: str, role: str = "user") -> dict:
    name = _clean_username(username)
    _check_password(password)
    if role not in USER_ROLES:
        raise UserError("Invalid role.")
    session = SessionLocal()
    try:
        repo = UserRepository(session)
        if repo.get_by_username(name):
            raise UserError(f"Username '{name}' already exists.")
        # New accounts must pick their own password at first login.
        user = repo.create(name, hash_password(password), role=role, must_change_password=True)
        session.commit()
        result = _to_dict(user)
    finally:
        session.close()
    log_action("CREATE USER", "user", result["id"], f"Created account '{name}' ({role})")
    return result


def set_active(user_id: int, active: bool, acting_user_id: int | None = None) -> dict:
    session = SessionLocal()
    try:
        repo = UserRepository(session)
        user = repo.get_by_id(user_id)
        if user is None:
            raise UserError("User not found.")
        if not active:
            if acting_user_id is not None and user.id == acting_user_id:
                raise UserError("You can't deactivate your own account.")
            if user.role == "admin" and user.is_active and repo.count_active_admins() <= 1:
                raise UserError("You can't deactivate the last active admin.")
        user.is_active = bool(active)
        session.commit()
        result = _to_dict(user)
    finally:
        session.close()
    log_action("ACTIVATE USER" if active else "DEACTIVATE USER", "user", user_id, f"Account '{result['username']}'")
    return result


def set_role(user_id: int, role: str, acting_user_id: int | None = None) -> dict:
    if role not in USER_ROLES:
        raise UserError("Invalid role.")
    session = SessionLocal()
    try:
        repo = UserRepository(session)
        user = repo.get_by_id(user_id)
        if user is None:
            raise UserError("User not found.")
        if user.role == "admin" and role != "admin" and user.is_active and repo.count_active_admins() <= 1:
            raise UserError("You can't remove admin rights from the last active admin.")
        user.role = role
        session.commit()
        result = _to_dict(user)
    finally:
        session.close()
    log_action("CHANGE ROLE", "user", user_id, f"Account '{result['username']}' is now {role}")
    return result


def reset_password(user_id: int, new_password: str) -> dict:
    """Admin reset — the user must choose a new password at next login."""
    _check_password(new_password)
    session = SessionLocal()
    try:
        user = UserRepository(session).get_by_id(user_id)
        if user is None:
            raise UserError("User not found.")
        user.password_hash = hash_password(new_password)
        user.must_change_password = True
        session.commit()
        result = _to_dict(user)
    finally:
        session.close()
    log_action("RESET PASSWORD", "user", user_id, f"Password reset for '{result['username']}'")
    return result


def change_own_password(user_id: int, old_password: str, new_password: str) -> dict:
    _check_password(new_password)
    session = SessionLocal()
    try:
        user = UserRepository(session).get_by_id(user_id)
        if user is None or not verify_password(old_password or "", user.password_hash):
            raise UserError("Current password is incorrect.")
        if verify_password(new_password, user.password_hash):
            raise UserError("New password must be different from the current one.")
        user.password_hash = hash_password(new_password)
        user.must_change_password = False
        session.commit()
        result = _to_dict(user)
    finally:
        session.close()
    log_action("CHANGE PASSWORD", "user", user_id, f"'{result['username']}' changed their password", username=result["username"])
    return result
