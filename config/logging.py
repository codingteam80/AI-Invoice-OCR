"""
Centralized logging setup. Import `get_logger(__name__)` anywhere in the app.
"""
import logging
import sys
from pathlib import Path
from config.settings import settings, BASE_DIR

LOG_DIR = Path(BASE_DIR) / "data" / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE = LOG_DIR / "app.log"

_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"


def _build_root_logger():
    root = logging.getLogger("ai_invoice_ocr")
    if root.handlers:
        return root  # already configured

    root.setLevel(settings.LOG_LEVEL)

    formatter = logging.Formatter(_FORMAT)

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(formatter)
    root.addHandler(console)

    try:
        file_handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)
    except OSError:
        pass  # read-only filesystem etc.

    return root


_build_root_logger()


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"ai_invoice_ocr.{name}")
