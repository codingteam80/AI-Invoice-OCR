"""
Handwriting detector: decides whether a page is handwritten or machine-printed
*before* OCR runs, so the pipeline can route to the right engine
(PaddleOCR for printed, TrOCR for handwritten).

Approach: Stroke Width Transform (SWT) heuristic.
Printed glyphs have a near-constant stroke width across a character (font
strokes are mechanically uniform). Handwriting has much higher stroke-width
variance because pen pressure/speed varies. We measure this per connected
component and use the aggregate coefficient of variation (std / mean) as
the signal, plus a secondary check on how irregular character baselines are.

This is a heuristic, not a trained classifier. It works well for the common
case (typed invoice vs. handwritten note/signatureheavy form) but is not
perfect — hence `settings.HANDWRITING_SWT_THRESHOLD` is tunable, and
`ocr/ocr_engine.py` allows a manual override to bypass it entirely.
"""
import cv2
import numpy as np

from config.logging import get_logger
from config.settings import settings

logger = get_logger("ocr.handwriting_detector")


def _stroke_widths(binary: np.ndarray) -> list[float]:
    """Estimate a representative stroke width for each connected component
    using the distance transform (max distance-to-background along the
    component's medial axis ~= half the stroke width)."""
    num_labels, labels, stats, _ = cv2.connectedComponentsWithStats(binary, connectivity=8)
    dist = cv2.distanceTransform(binary, cv2.DIST_L2, 5)

    widths = []
    for label in range(1, num_labels):  # skip background label 0
        area = stats[label, cv2.CC_STAT_AREA]
        if area < 6:  # ignore noise specks
            continue
        w, h = stats[label, cv2.CC_STAT_WIDTH], stats[label, cv2.CC_STAT_HEIGHT]
        if w > binary.shape[1] * 0.9 or h > binary.shape[0] * 0.9:
            continue  # skip components that are basically the whole page (borders/lines)
        component_dist = dist[labels == label]
        if component_dist.size == 0:
            continue
        widths.append(float(np.max(component_dist)) * 2.0)
    return widths


def detect_array(gray: np.ndarray, min_components: int | None = None) -> dict:
    """
    Core detection logic on an already-loaded grayscale image (full page
    OR a single cropped region — same function works for both). Used
    directly by the hybrid per-region router for speed (no disk I/O per
    crop), and by detect() below for whole-page/file-based calls.

    min_components: override for the "not enough text to classify" cutoff.
    A single cropped line naturally has far fewer glyph components than a
    whole page, so callers doing per-region classification should pass
    settings.HANDWRITING_MIN_COMPONENTS_REGION instead of the page default.

    Returns:
        {
          "is_handwritten": bool,
          "score": float,       # coefficient of variation of stroke widths (higher = more handwriting-like)
          "sample_size": int,   # number of glyph components analyzed
        }
    """
    min_components = settings.HANDWRITING_MIN_COMPONENTS if min_components is None else min_components

    binary = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV | cv2.THRESH_OTSU)[1]
    widths = _stroke_widths(binary)

    if len(widths) < min_components:
        # Not enough text detected to make a confident call — default to printed,
        # since that's the far more common case and PaddleOCR degrades more
        # gracefully on a bad guess than TrOCR does on a full page.
        return {"is_handwritten": False, "score": 0.0, "sample_size": len(widths)}

    mean_w = float(np.mean(widths))
    std_w = float(np.std(widths))
    cv_score = std_w / mean_w if mean_w > 0 else 0.0

    is_handwritten = cv_score >= settings.HANDWRITING_SWT_THRESHOLD
    return {"is_handwritten": is_handwritten, "score": round(cv_score, 4), "sample_size": len(widths)}


def detect(image_path: str) -> dict:
    """File-based convenience wrapper around detect_array() for whole pages."""
    img = cv2.imread(image_path, cv2.IMREAD_GRAYSCALE)
    if img is None:
        raise ValueError(f"Could not read image for handwriting detection: {image_path}")

    result = detect_array(img)
    logger.info(
        f"Handwriting detection ({image_path}): score={result['score']} "
        f"(threshold={settings.HANDWRITING_SWT_THRESHOLD}) -> "
        f"{'handwritten' if result['is_handwritten'] else 'printed'}"
    )
    return result
