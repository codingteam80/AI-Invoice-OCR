"""PaddleOCR-backed text extraction engine (primary OCR engine)."""
from functools import lru_cache
from config.logging import get_logger
from config.settings import settings

logger = get_logger("ocr.paddleocr")


@lru_cache(maxsize=1)
def _get_paddle_instance():
    from paddleocr import PaddleOCR
    logger.info(f"Loading PaddleOCR (lang={settings.OCR_LANG})...")
    return PaddleOCR(use_angle_cls=True, lang=settings.OCR_LANG, show_log=False)


def extract_text(image_path: str) -> dict:
    """
    Run full PaddleOCR (detection + recognition) on an image.
    Used for whole-page printed docs (force_handwritten=False), or as a
    fallback when hybrid per-region routing fails.

    Returns:
        {
          "text": "<full concatenated text>",
          "lines": [{"text": str, "confidence": float, "bbox": [[x,y],...]}],
          "avg_confidence": float
        }
    """
    ocr = _get_paddle_instance()
    result = ocr.ocr(image_path, cls=True)

    lines = []
    confidences = []
    full_text_parts = []

    for page in result or []:
        for entry in page or []:
            bbox, (text, conf) = entry
            lines.append({"text": text, "confidence": float(conf), "bbox": bbox})
            confidences.append(conf)
            full_text_parts.append(text)

    avg_conf = sum(confidences) / len(confidences) if confidences else 0.0

    return {
        "text": "\n".join(full_text_parts),
        "lines": lines,
        "avg_confidence": round(avg_conf, 4),
    }


def detect_boxes(image_path: str) -> list:
    """
    Detection-only pass: returns text-region bounding boxes without running
    recognition. Used by the hybrid per-region router (ocr/ocr_engine.py)
    and by TrOCR (which has no detector of its own) to find where the
    text lines are before deciding, per region, which recognizer to use.
    """
    ocr = _get_paddle_instance()
    result = ocr.ocr(image_path, cls=True, rec=False)
    raw = result[0] if result else []
    # Depending on paddleocr version, rec=False entries are either a bare
    # bbox or a (bbox, None) tuple — normalize to a plain list of bboxes.
    return [entry[0] if isinstance(entry, (list, tuple)) and len(entry) == 2
            and not isinstance(entry[0][0], (int, float)) else entry
            for entry in raw]


def recognize_crop(crop) -> dict:
    """
    Recognition-only pass on an already-cropped line/word image (numpy
    array, BGR). Used by the hybrid router to read a single region that's
    already been classified as printed text.

    Returns: {"text": str, "confidence": float}
    """
    ocr = _get_paddle_instance()
    result = ocr.ocr(crop, cls=True, det=False, rec=True)
    if not result or not result[0]:
        return {"text": "", "confidence": 0.0}
    text, conf = result[0][0]
    return {"text": text, "confidence": float(conf)}
