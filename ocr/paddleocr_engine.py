"""PaddleOCR-backed text extraction engine (primary OCR engine)."""
from functools import lru_cache
from config.logging import get_logger
from config.settings import settings

logger = get_logger("ocr.paddleocr")


@lru_cache(maxsize=1)
def _get_paddle_instance():
    from paddleocr import PaddleOCR
    kwargs = dict(use_angle_cls=True, lang=settings.OCR_LANG, show_log=False)
    if settings.OCR_DET_LIMIT_SIDE_LEN is not None:
        kwargs["det_limit_side_len"] = settings.OCR_DET_LIMIT_SIDE_LEN
    side_len_desc = kwargs.get("det_limit_side_len", "library default (960)")
    logger.info(f"Loading PaddleOCR (lang={settings.OCR_LANG}, det_limit_side_len={side_len_desc})...")
    return PaddleOCR(**kwargs)


def _reading_order_key(line: dict, bucket_height: float) -> tuple:
    """Sort top-to-bottom, then left-to-right, tolerating small y jitter
    between glyphs on the same visual line.

    bucket_height is shared across the whole page (see _median_line_height)
    rather than each line computing its own — using each line's own height
    let two boxes that are genuinely side-by-side on the same physical row
    (e.g. an item name and its price) round to different y-buckets whenever
    their two heights happened to differ slightly, occasionally splitting
    a single row into two."""
    ys = [p[1] for p in line["bbox"]]
    xs = [p[0] for p in line["bbox"]]
    y_center = sum(ys) / len(ys)
    x_center = sum(xs) / len(xs)
    return (round(y_center / bucket_height), x_center)


def _median_line_height(lines: list[dict]) -> float:
    heights = [max(p[1] for p in l["bbox"]) - min(p[1] for p in l["bbox"]) for l in lines]
    heights = [h for h in heights if h > 0]
    if not heights:
        return 20.0
    heights.sort()
    return max(heights[len(heights) // 2], 1.0)


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

    for page in result or []:
        for entry in page or []:
            bbox, (text, conf) = entry
            lines.append({"text": text, "confidence": float(conf), "bbox": bbox})
            confidences.append(conf)

    # Sort into top-to-bottom, left-to-right reading order before joining —
    # PaddleOCR's own detection-result order is NOT reading order (it
    # reflects whatever internal box-proposal order its detector happened
    # to produce, which can start anywhere on the page). Confirmed on real
    # North Star Travel invoices: the resulting `text` started with the
    # tiny "LL No.LLAR-049-06/2024-001213 Date Issued: June 20, 2024"
    # boilerplate line printed at the very BOTTOM of the page, and ended
    # with "NORTH STAR INTERNATIONAL TRAVEL INC." from the very TOP — the
    # actual invoice number ("No. 52091", also near the top) got buried in
    # the middle. Since ai/extractor.py's prompt presents this text to the
    # LLM roughly in the order it appears, a scrambled reading order
    # doesn't just look odd — it actively misleads which "No."-labeled
    # value the model treats as most salient/first-encountered, and
    # disrupts the LABEL-VALUE proximity that field extraction depends on
    # throughout the whole document, not just for invoice_number.
    #
    # This exact sort (_reading_order_key/_median_line_height) already
    # existed and was applied correctly in ocr/ocr_engine.py's hybrid
    # per-region extraction path (see _hybrid_extract) — it just wasn't
    # ALSO applied here, in the plain whole-page PaddleOCR path, which is
    # the one actually used for most printed (non-handwriting-routed)
    # documents (ocr_engine_used: "paddleocr", not "paddleocr+trocr").
    # Moved the two helpers here as the single shared implementation so
    # both callers apply the identical sort — see ocr/ocr_engine.py, which
    # now imports them from this module instead of duplicating them.
    if lines:
        bucket_height = _median_line_height(lines)
        lines.sort(key=lambda l: _reading_order_key(l, bucket_height))

    avg_conf = sum(confidences) / len(confidences) if confidences else 0.0

    return {
        "text": "\n".join(l["text"] for l in lines),
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
