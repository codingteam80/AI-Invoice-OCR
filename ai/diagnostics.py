"""Per-invoice extraction diagnostics.

Writes a JSON trace that preserves what each stage saw and produced:
OCR regions/coordinates/confidence, text-LLM raw/parsed responses and token
counts, vision-LLM raw/parsed responses and corrections, reconciliation
notes, validation issues, and final values. Best-effort only: diagnostics
must never make invoice processing fail.
"""
from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from config.logging import get_logger
from config.settings import settings
from utils.file_utils import sanitize_filename_component

logger = get_logger("ai.diagnostics")


def _jsonable(value: Any):
    if isinstance(value, Path):
        return str(value)
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return value


def save_diagnostic_trace(
    *,
    source_file: str,
    page_number: int | None,
    image_path: str | None,
    processed_image_path: str | None,
    ocr_result: dict | None,
    text_llm_trace: dict | None,
    vision_trace: dict | None,
    stages: dict | None,
    validation_issues: list[str] | None,
    final_data: Any = None,
    error: str | None = None,
) -> str | None:
    if not getattr(settings, "DIAGNOSTIC_TRACE_ENABLED", True):
        return None
    try:
        debug_root = Path(settings.DEBUG_DIR)
        debug_root.mkdir(parents=True, exist_ok=True)
        stem = sanitize_filename_component(Path(source_file).stem or "invoice")
        page = f"_p{page_number}" if page_number else ""
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")
        run_dir = debug_root / f"{stem}{page}_{stamp}"
        run_dir.mkdir(parents=True, exist_ok=True)

        copied_images: dict[str, str | None] = {"original": None, "ocr_input": None}
        for label, src in (("original", image_path), ("ocr_input", processed_image_path)):
            if not src:
                continue
            p = Path(src)
            if p.exists() and p.is_file():
                dest = run_dir / f"{label}{p.suffix or '.png'}"
                shutil.copy2(p, dest)
                copied_images[label] = str(dest)

        trace = {
            "schema_version": 1,
            "created_at_utc": datetime.now(timezone.utc).isoformat(),
            "source_file": source_file,
            "page_number": page_number,
            "images": copied_images,
            "ocr": _jsonable(ocr_result or {}),
            "text_llm": _jsonable(text_llm_trace or {}),
            "vision_llm": _jsonable(vision_trace or {}),
            "pipeline_stages": _jsonable(stages or {}),
            "validation_issues": validation_issues or [],
            "final": _jsonable(final_data),
            "error": error,
        }
        out = run_dir / "trace.json"
        out.write_text(json.dumps(trace, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
        return str(out)
    except Exception as exc:
        logger.warning(f"Could not write diagnostic trace: {exc}")
        return None
