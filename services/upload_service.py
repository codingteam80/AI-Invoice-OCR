"""Handles incoming file uploads: validation, dedup-check, saving to disk."""
from config.settings import settings
from config.logging import get_logger
from utils.file_utils import is_supported_file, save_upload, file_hash, file_size_mb

logger = get_logger("services.upload")


class UploadError(Exception):
    pass


def handle_upload(file_bytes: bytes, filename: str) -> str:
    if not is_supported_file(filename):
        raise UploadError(f"Unsupported file type: {filename}")

    size_mb = len(file_bytes) / (1024 * 1024)
    if size_mb > settings.MAX_UPLOAD_MB:
        raise UploadError(f"File too large ({size_mb:.1f} MB > {settings.MAX_UPLOAD_MB} MB limit)")

    saved_path = save_upload(file_bytes, filename)
    logger.info(f"Saved upload '{filename}' -> {saved_path} ({file_hash(saved_path)[:12]}...)")
    return saved_path
