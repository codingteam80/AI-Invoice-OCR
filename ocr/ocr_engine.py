"""
Unified OCR interface. For each document, detects text regions once, then
classifies and recognizes EACH region independently:
  - a region that looks printed  -> PaddleOCR
  - a region that looks handwritten -> TrOCR

This per-region routing (rather than one verdict for the whole page)
is what lets a single invoice mix printed body text with a handwritten
signature, annotation, or total and still get both parts read correctly.

Manual overrides (`force_handwritten=True/False`) skip detection and
classification entirely and pin the WHOLE page to one engine — useful
when a caller already knows a document is uniformly printed or uniformly
handwritten and wants to avoid the extra per-region overhead.
"""
from pathlib import Path

import torch  # noqa: F401 — imported eagerly and first, before paddleocr/paddlepaddle
# ever loads. Both torch and paddlepaddle bundle their own native
# OpenMP/MKL runtime DLLs; on Windows, whichever one's DLLs get loaded
# into the process first "wins" the DLL search path, and the second one
# to load can fail with OSError: [WinError 127] ... shm.dll (or similar).
# Both ocr/paddleocr_engine.py and ocr/trocr_engine.py import their real
# libraries lazily (only when first used), and _hybrid_extract() below
# always calls PaddleOCR before TrOCR — so without this eager import,
# paddle would win the race on every run and torch would be the one to
# fail. Importing torch here, at module load time (before any OCR call),
# ensures torch's DLLs are loaded first instead.
import cv2
import numpy as np
from PIL import Image

from config.logging import get_logger
from config.settings import settings
from ocr import paddleocr_engine, trocr_engine, handwriting_detector
from ocr.preprocessing import preprocess
from utils.pdf_utils import pdf_to_images

logger = get_logger("ocr.engine")


def _crop_bgr(img_bgr: np.ndarray, bbox: list) -> np.ndarray:
    xs = [p[0] for p in bbox]
    ys = [p[1] for p in bbox]
    left, right = max(0, int(min(xs))), int(max(xs))
    top, bottom = max(0, int(min(ys))), int(max(ys))
    if right <= left or bottom <= top:
        return img_bgr
    return img_bgr[top:bottom, left:right]


def _reading_order_key(line: dict) -> tuple:
    """Sort top-to-bottom, then left-to-right, tolerating small y jitter
    between glyphs on the same visual line."""
    ys = [p[1] for p in line["bbox"]]
    xs = [p[0] for p in line["bbox"]]
    y_center = sum(ys) / len(ys)
    x_center = sum(xs) / len(xs)
    line_height = max(ys) - min(ys) or 1
    return (round(y_center / max(line_height, 20)), x_center)


def _assign_columns(lines: list[dict], page_width: float, gap_ratio: float = 0.06) -> list[int]:
    """
    Cluster boxes into columns by x-position (e.g. a left "client info"
    column vs a right "invoice metadata" column), using a simple 1D gap
    scan over x-centers rather than a fixed column count.

    Sorting purely by (y_bucket, x) — the previous behaviour — silently
    breaks on multi-column templates: on a two-column invoice, two boxes
    that are logically unrelated (one from each column) can land in the
    same y-bucket purely from page skew/curvature (very common on phone
    photos of a notebook, like a curled page), and then get interleaved
    by x instead of being read one column fully, then the next. That
    interleaving is what scrambles output like "Due Date" appearing next
    to "quantity" instead of both invoice-metadata lines staying together.

    This clusters x-centers into columns first (any gap between sorted
    x-centers wider than `gap_ratio` of the page width starts a new
    column), then reading order sorts by (column, y) within each column
    — so a whole column is read top-to-bottom before moving to the next.
    Single-column documents are unaffected: everything just falls into
    column 0.
    """
    if not lines:
        return []
    x_centers = [sum(p[0] for p in l["bbox"]) / len(l["bbox"]) for l in lines]
    order = sorted(range(len(lines)), key=lambda i: x_centers[i])
    gap_threshold = max(page_width * gap_ratio, 1.0)

    columns = [0] * len(lines)
    current_col = 0
    for prev_i, cur_i in zip(order, order[1:]):
        if x_centers[cur_i] - x_centers[prev_i] > gap_threshold:
            current_col += 1
        columns[cur_i] = current_col
    return columns


def _hybrid_extract(image_path: str) -> dict:
    """
    Detect all text regions once, classify each region individually as
    printed or handwritten, and recognize it with the matching engine.
    """
    boxes = paddleocr_engine.detect_boxes(image_path)
    if not boxes:
        logger.warning(f"No text regions detected in {image_path}, falling back to full-page PaddleOCR")
        result = paddleocr_engine.extract_text(image_path)
        result["engine"] = "paddleocr"
        result["region_summary"] = {"printed": 0, "handwritten": 0}
        return result

    img_bgr = cv2.imread(image_path)
    img_rgb_pil = Image.open(image_path).convert("RGB")

    lines = []
    region_counts = {"printed": 0, "handwritten": 0}

    for bbox in boxes:
        crop_bgr = _crop_bgr(img_bgr, bbox)
        if crop_bgr.size == 0:
            continue
        crop_gray = cv2.cvtColor(crop_bgr, cv2.COLOR_BGR2GRAY)

        try:
            classification = handwriting_detector.detect_array(
                crop_gray, min_components=settings.HANDWRITING_MIN_COMPONENTS_REGION
            )
            is_handwritten = classification["is_handwritten"]
        except Exception as e:
            logger.warning(f"Region classification failed ({e}), defaulting region to printed")
            is_handwritten = False

        if is_handwritten:
            crop_pil = img_rgb_pil.crop(_bbox_pil_box(bbox))
            res = trocr_engine.recognize_crop(crop_pil)
            engine_tag = "trocr"
        else:
            res = paddleocr_engine.recognize_crop(crop_bgr)
            engine_tag = "paddleocr"

        if not res["text"]:
            continue

        region_counts["handwritten" if is_handwritten else "printed"] += 1
        lines.append({
            "text": res["text"],
            "confidence": res["confidence"],
            "bbox": bbox,
            "engine": engine_tag,
        })

    page_width = img_bgr.shape[1] if img_bgr is not None else 0
    columns = _assign_columns(lines, page_width)
    for line, col in zip(lines, columns):
        line["_column"] = col

    def _column_reading_order_key(line: dict) -> tuple:
        ys = [p[1] for p in line["bbox"]]
        line_height = max(ys) - min(ys) or 1
        return (line["_column"], round(sum(ys) / len(ys) / max(line_height, 20)))

    lines.sort(key=_column_reading_order_key)
    for line in lines:
        line.pop("_column", None)

    confidences = [l["confidence"] for l in lines]
    engines_used = sorted(set(l["engine"] for l in lines))

    return {
        "text": "\n".join(l["text"] for l in lines),
        "lines": lines,
        "avg_confidence": round(sum(confidences) / len(confidences), 4) if confidences else 0.0,
        "engine": "+".join(engines_used) if engines_used else "paddleocr",
        "region_summary": region_counts,
    }


def _bbox_pil_box(bbox: list) -> tuple:
    xs = [p[0] for p in bbox]
    ys = [p[1] for p in bbox]
    return (max(0, int(min(xs))), max(0, int(min(ys))), int(max(xs)), int(max(ys)))


def extract_from_image(
    image_path: str,
    use_preprocessing: bool = False,
    force_handwritten: bool | None = None,
) -> dict:
    """
    Run OCR on a single image.

    force_handwritten:
        None  -> auto (default): classify and route EACH text region
                 independently, so mixed printed/handwritten pages work.
        True  -> skip detection, pin the WHOLE page to TrOCR.
        False -> skip detection, pin the WHOLE page to PaddleOCR.
    """
    target_path = image_path

    if use_preprocessing:
        try:
            processed_path = str(Path(settings.TEMP_DIR) / f"pre_{Path(image_path).name}")
            preprocess(image_path, save_path=processed_path, binarize=settings.OCR_PREPROCESS_BINARIZE)
            target_path = processed_path
        except Exception as e:
            logger.warning(f"Preprocessing failed, using original image: {e}")

    if force_handwritten is True:
        result = trocr_engine.extract_text(target_path)
        result["engine"] = "trocr"
        return result

    if force_handwritten is False:
        result = paddleocr_engine.extract_text(target_path)
        result["engine"] = "paddleocr"
        return result

    try:
        return _hybrid_extract(target_path)
    except Exception as e:
        logger.warning(f"Hybrid per-region OCR failed ({e}), falling back to full-page PaddleOCR")
        result = paddleocr_engine.extract_text(target_path)
        result["engine"] = "paddleocr_fallback"
        return result


def extract_from_file(file_path: str, force_handwritten: bool | None = None) -> dict:
    """
    Handles both images and PDFs. For multi-page PDFs, concatenates text
    from all pages and averages confidence. Each page (and each region
    within it) is independently routed unless `force_handwritten` pins the
    whole document to one engine.
    """
    ext = Path(file_path).suffix.lower()

    if ext == ".pdf":
        image_paths = pdf_to_images(file_path)
        all_text, all_lines, confs, engines_used = [], [], [], set()
        for img_path in image_paths:
            res = extract_from_image(img_path, force_handwritten=force_handwritten)
            all_text.append(res["text"])
            all_lines.extend(res["lines"])
            confs.append(res["avg_confidence"])
            engines_used.update(res["engine"].split("+"))
        return {
            "text": "\n\n".join(all_text),
            "lines": all_lines,
            "avg_confidence": round(sum(confs) / len(confs), 4) if confs else 0.0,
            "engine": "+".join(sorted(engines_used)) if engines_used else None,
            "pages": len(image_paths),
        }

    return extract_from_image(file_path, force_handwritten=force_handwritten)
