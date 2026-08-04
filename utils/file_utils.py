"""File-system helper functions: saving uploads, hashing, validation."""
import hashlib
import shutil
import uuid
from pathlib import Path
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
