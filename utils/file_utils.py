"""File-system helper functions: saving uploads, hashing, validation."""
import hashlib
import shutil
import uuid
from pathlib import Path, PureWindowsPath, PurePosixPath
from config.constants import SUPPORTED_EXTS
from config.settings import settings


def is_supported_file(filename: str) -> bool:
    return Path(filename).suffix.lower() in SUPPORTED_EXTS


def save_upload(file_bytes: bytes, original_filename: str) -> str:
    """Save uploaded bytes into UPLOAD_DIR with a unique name; returns saved path."""
    ext = Path(original_filename).suffix.lower()
    unique_name = f"{uuid.uuid4().hex}{ext}"
    dest = Path(settings.UPLOAD_DIR) / unique_name
    dest.write_bytes(file_bytes)
    return str(dest)


def file_hash(path: str) -> str:
    """SHA-256 hash — used to detect duplicate invoice uploads."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def move_to_processed(path: str) -> str:
    dest = Path(settings.PROCESSED_DIR) / Path(path).name
    shutil.move(path, dest)
    return str(dest)


def file_size_mb(path: str) -> float:
    return Path(path).stat().st_size / (1024 * 1024)


def resolve_source_file(stored_path: str | None) -> Path | None:
    """Best-effort lookup for an invoice's saved image/PDF on disk.

    `source_file` is persisted at processing time as a plain absolute path.
    That path can go stale — most commonly because the record was created
    on a different machine/OS than the one currently serving the app (e.g.
    a Windows dev path like 'C:\\...\\data\\uploads\\xyz.jpg' baked into the
    DB before deploying to a Linux server), or because the file was moved
    after the record was written. Rather than only checking the literal
    path, this also looks for a same-named file in the app's own upload/
    processed/temp folders, which is where it actually lives today.
    """
    if not stored_path:
        return None

    direct = Path(stored_path)
    if direct.exists():
        return direct

    # Handle a foreign OS path (e.g. Windows backslashes) arriving as a
    # single opaque string on Linux/Mac — pull out just the filename.
    filename = PureWindowsPath(stored_path).name or PurePosixPath(stored_path).name
    if not filename:
        return None

    for folder in (settings.PROCESSED_DIR, settings.UPLOAD_DIR, settings.TEMP_DIR):
        candidate = Path(folder) / filename
        if candidate.exists():
            return candidate

    return None
