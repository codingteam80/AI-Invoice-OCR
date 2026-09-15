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
from ocr.preprocessing import preprocess, rotate_90
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


def _reading_order_key(line: dict, bucket_height: float) -> tuple:
    """Thin re-export — see ocr/paddleocr_engine.py for the implementation
    and full rationale. Kept here so existing call sites below don't need
    updating, but paddleocr_engine.py is the single source of truth (its
    own extract_text() needs the same sort and can't import it back from
    here without a circular import)."""
    return paddleocr_engine._reading_order_key(line, bucket_height)


def _median_line_height(lines: list[dict]) -> float:
    """Thin re-export — see ocr/paddleocr_engine.py."""
    return paddleocr_engine._median_line_height(lines)


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
            try:
                res = trocr_engine.recognize_crop(crop_pil)
            except Exception as e:
                # Confirmed real failure mode: a single degenerate/tiny
                # crop (very common on messy handwritten layouts, e.g. a
                # real Gliptic Art Enterprise invoice) can make TrOCR throw
                # — a shape mismatch in the vision transformer's patch
                # embedding on a too-small crop, a decoder error, etc.
                # Before this fix, that ONE bad region's exception was
                # UNCAUGHT here, so it propagated all the way out of
                # _hybrid_extract() and was caught only by the outer
                # try/except in extract_from_file() — which discarded every
                # OTHER successfully-read region on the page (often the
                # large majority of them) and fell back to whole-page plain
                # PaddleOCR, which can't read handwriting at all. Skipping
                # just this one region keeps everything else the page
                # already read correctly.
                logger.warning(f"TrOCR recognition failed for one region ({e}), skipping that region")
                continue
            engine_tag = "trocr"
        else:
            try:
                res = paddleocr_engine.recognize_crop(crop_bgr)
            except Exception as e:
                # Same reasoning as the TrOCR branch above — confirmed real
                # failure mode on a sideways-scanned page (e.g. a real SGV
                # Gorres Velayo invoice photographed in landscape): a
                # rotated/degenerate crop can make PaddleOCR's recognizer
                # throw (e.g. a near-zero-height crop breaking its internal
                # resize step) instead of just returning low confidence.
                logger.warning(f"PaddleOCR recognition failed for one region ({e}), skipping that region")
                continue
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

    # Row-major reading order: top-to-bottom, then left-to-right within
    # each row band (see _reading_order_key/_median_line_height above).
    #
    # An earlier version of this function tried to detect genuine 2-column
    # layouts (e.g. a "Bill To" block next to an "Invoice Date/Terms"
    # block) and read column-by-column instead. That was removed: on a
    # receipt, an item row is itself two x-groups on the same line
    # ("ITEM NAME .......... P225.00" — label far left, price far right),
    # and any gap-based column detector reliably mistakes that for a page
    # column boundary. The result was reading ALL item labels top-to-
    # bottom, then ALL prices top-to-bottom, completely severing every
    # item from its own price — which is silently self-consistent enough
    # to survive the subtotal cross-check in ai/validator.py and produce a
    # wrong-but-confident extraction (e.g. a barcode number ending up as
    # an item "description", or the real subtotal being read as a 3rd
    # line item's price).
    #
    # A workable "is this really two blocks, or one table row" signal
    # would need actual layout/semantic understanding, not just bounding
    # box geometry — even a neatly template-aligned 2-column header (both
    # blocks starting at nearly the same y, which is common) is
    # geometrically indistinguishable from a table row. Given that
    # trade-off, this deliberately favors getting line-item tables and
    # receipts right (the core feature) over the rarer case of a 2-column
    # header occasionally reading interleaved — which the extraction
    # prompt's per-field label matching, plus ai/validator.py's
    # date/terms cross-check, already partially compensate for anyway.
    bucket_height = _median_line_height(lines)
    lines.sort(key=lambda l: _reading_order_key(l, bucket_height))

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


def _bbox_area(bbox: list) -> float:
    xs = [p[0] for p in bbox]
    ys = [p[1] for p in bbox]
    return max(0.0, max(xs) - min(xs)) * max(0.0, max(ys) - min(ys))


def _orientation_score(image_path: str) -> tuple[float, dict]:
    """Score a candidate page orientation by *readable text quality*.

    Detection count alone is unreliable for sideways pages: Paddle can detect
    many vertical boxes but recognize almost no useful text. We therefore
    recognize a capped sample of detected regions and reward meaningful words,
    alphanumeric characters, confidence, and horizontal geometry.
    """
    try:
        boxes = paddleocr_engine.detect_boxes(image_path)
    except Exception as e:
        logger.warning(f"Orientation probe failed for {image_path}: {e}")
        return 0.0, {"boxes": 0, "readable": 0, "chars": 0, "avg_conf": 0.0}
    if not boxes:
        return 0.0, {"boxes": 0, "readable": 0, "chars": 0, "avg_conf": 0.0}

    img = cv2.imread(image_path)
    if img is None:
        return 0.0, {"boxes": len(boxes), "readable": 0, "chars": 0, "avg_conf": 0.0}

    # Largest regions are most informative; cap work so auto-orient stays cheap.
    sample = sorted(boxes, key=_bbox_area, reverse=True)[:24]
    readable = chars = words = horizontal = 0
    confs = []
    for bbox in sample:
        try:
            xs=[float(p[0]) for p in bbox]; ys=[float(p[1]) for p in bbox]
            w=max(xs)-min(xs); h=max(ys)-min(ys)
            if w >= h * 1.15:
                horizontal += 1
            crop = _crop_bgr(img, bbox)
            if crop.size == 0:
                continue
            res = paddleocr_engine.recognize_crop(crop)
            text = str(res.get("text") or "").strip()
            conf = float(res.get("confidence") or 0.0)
            meaningful = [c for c in text if c.isalnum()]
            alpha_words = [w for w in text.split() if sum(ch.isalpha() for ch in w) >= 2]
            if len(meaningful) >= 2:
                readable += 1
                chars += len(meaningful)
                words += len(alpha_words)
                confs.append(conf)
        except Exception:
            continue
    avg_conf = sum(confs)/len(confs) if confs else 0.0
    # Readability dominates; geometry is only supporting evidence.
    score = readable*12.0 + words*3.0 + min(chars, 300)*0.22 + avg_conf*20.0 + horizontal*0.8
    return score, {"boxes": len(boxes), "readable": readable, "chars": chars,
                   "words": words, "avg_conf": round(avg_conf,4), "horizontal": horizontal}


_ORIENTATION_MIN_SCORE_RATIO = 1.18
_ORIENTATION_MIN_SCORE_MARGIN = 12.0


def _detect_page_orientation(image_path: str) -> int:
    """Try 0/90/180/270 and choose a rotation only when recognized-text
    quality is clearly better than the uploaded orientation."""
    img = cv2.imread(image_path)
    if img is None:
        return 0
    scores = {}
    for quarter_turns in range(4):
        candidate_path=image_path; tmp_path=None
        if quarter_turns:
            rotated=rotate_90(img, quarter_turns)
            tmp_path=str(Path(settings.TEMP_DIR) / f"orient_probe_{quarter_turns}_{Path(image_path).name}")
            cv2.imwrite(tmp_path, rotated); candidate_path=tmp_path
        scores[quarter_turns]=_orientation_score(candidate_path)
        if tmp_path:
            try: Path(tmp_path).unlink(missing_ok=True)
            except OSError: pass

    base=scores[0][0]
    best_turn=max(scores, key=lambda k: scores[k][0])
    best=scores[best_turn][0]
    if best_turn and best >= base*_ORIENTATION_MIN_SCORE_RATIO and best >= base+_ORIENTATION_MIN_SCORE_MARGIN:
        logger.info(f"Auto-rotated {image_path} by {best_turn*90} degrees clockwise; "
                    f"readability score {base:.1f}->{best:.1f}; probes={scores}")
        return best_turn
    logger.info(f"Auto-orientation kept 0 degrees; probes={scores}")
    return 0

def correct_page_orientation(image_path: str, save_path: str | None = None, return_turns: bool = False):
    """
    Detects and corrects a full 90/180/270-degree page rotation (see
    _detect_page_orientation above). Returns image_path unchanged if the
    page is already upright or orientation correction is disabled via
    settings.OCR_AUTO_ORIENT_ENABLED; otherwise writes the rotated image to
    `save_path` (or a temp-dir path if not given) and returns that path.
    """
    if not settings.OCR_AUTO_ORIENT_ENABLED:
        return (image_path, 0) if return_turns else image_path

    quarter_turns = _detect_page_orientation(image_path)
    if quarter_turns == 0:
        return (image_path, 0) if return_turns else image_path

    img = cv2.imread(image_path)
    if img is None:
        return (image_path, 0) if return_turns else image_path
    rotated = rotate_90(img, quarter_turns)

    out_path = save_path or str(Path(settings.TEMP_DIR) / f"oriented_{Path(image_path).name}")
    cv2.imwrite(out_path, rotated)
    return (out_path, quarter_turns) if return_turns else out_path


def extract_from_image(
    image_path: str,
    use_preprocessing: bool | None = None,
    force_handwritten: bool | None = None,
) -> dict:
    """
    Run OCR on a single image.

    force_handwritten:
        None  -> auto (default): classify and route EACH text region
                 independently, so mixed printed/handwritten pages work.
        True  -> skip detection, pin the WHOLE page to TrOCR.
        False -> skip detection, pin the WHOLE page to PaddleOCR.

    use_preprocessing:
        None  -> (default) follow settings.OCR_PREPROCESS_ENABLED, so the
                 deskew/denoise/contrast-enhancement pipeline in
                 ocr/preprocessing.py actually runs unless the user turned
                 it off. Explicit True/False overrides the setting.
    """
    target_path = image_path
    orientation_turns = 0
    oriented_path = image_path
    if use_preprocessing is None:
        use_preprocessing = settings.OCR_PREPROCESS_ENABLED

    # Whole-page 90/180/270 rotation correction runs FIRST, before deskew/
    # denoise/contrast — deskew() only handles small camera-tilt angles and
    # would otherwise be trying to fine-tune a page that's fundamentally
    # sideways to begin with. See correct_page_orientation() above.
    try:
        target_path, orientation_turns = correct_page_orientation(target_path, return_turns=True)
        oriented_path = target_path
    except Exception as e:
        logger.warning(f"Page orientation correction failed, using original image: {e}")

    if use_preprocessing:
        try:
            processed_path = str(Path(settings.TEMP_DIR) / f"pre_{Path(target_path).name}")
            preprocess(target_path, save_path=processed_path, binarize=settings.OCR_PREPROCESS_BINARIZE)
            target_path = processed_path
        except Exception as e:
            logger.warning(f"Preprocessing failed, using original image: {e}")

    if force_handwritten is True:
        result = trocr_engine.extract_text(target_path)
        result["engine"] = "trocr"
        result.update({"original_image_path": image_path, "oriented_image_path": oriented_path, "processed_image_path": target_path, "orientation_quarter_turns": orientation_turns})
        return result

    if force_handwritten is False:
        result = paddleocr_engine.extract_text(target_path)
        result["engine"] = "paddleocr"
        result.update({"original_image_path": image_path, "oriented_image_path": oriented_path, "processed_image_path": target_path, "orientation_quarter_turns": orientation_turns})
        return result

    try:
        result = _hybrid_extract(target_path)
        result.update({"original_image_path": image_path, "oriented_image_path": oriented_path, "processed_image_path": target_path, "orientation_quarter_turns": orientation_turns})
        return result
    except Exception as e:
        logger.warning(f"Hybrid per-region OCR failed ({e}), falling back to full-page PaddleOCR")
        result = paddleocr_engine.extract_text(target_path)
        result["engine"] = "paddleocr_fallback"
        result.update({"original_image_path": image_path, "oriented_image_path": oriented_path, "processed_image_path": target_path, "orientation_quarter_turns": orientation_turns})
        return result


def extract_from_file(
    file_path: str,
    force_handwritten: bool | None = None,
    use_preprocessing: bool | None = None,
) -> dict:
    """
    Handles both images and PDFs. For multi-page PDFs, concatenates text
    from all pages and averages confidence. Each page (and each region
    within it) is independently routed unless `force_handwritten` pins the
    whole document to one engine.

    use_preprocessing: None (default) follows settings.OCR_PREPROCESS_ENABLED
    — see extract_from_image().
    """
    ext = Path(file_path).suffix.lower()

    if ext == ".pdf":
        image_paths = pdf_to_images(file_path)
        all_text, all_lines, confs, engines_used = [], [], [], set()
        for img_path in image_paths:
            res = extract_from_image(
                img_path, use_preprocessing=use_preprocessing, force_handwritten=force_handwritten
            )
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

    return extract_from_image(file_path, use_preprocessing=use_preprocessing, force_handwritten=force_handwritten)


def extract_pages_from_file(
    file_path: str,
    force_handwritten: bool | None = None,
    use_preprocessing: bool | None = None,
) -> list[dict]:
    """
    Like extract_from_file(), but returns OCR results PER PAGE instead of
    concatenating every page into one block of text.

    Needed because a multi-page PDF is not always one invoice spread across
    several pages — it's often several SEPARATE invoices stacked into one
    file (e.g. a batch scan of a stack of receipts, or one PDF export
    containing two unrelated service invoices). extract_from_file()'s
    "join every page's text together" behavior silently collapses a
    multi-invoice PDF into a single LLM extraction call, which can only
    return one invoice's worth of fields — every invoice but whichever one
    "won" the extraction gets dropped entirely, with no error or warning.
    See parser/invoice_parser.py::parse_invoice_pages, which calls this and
    runs the full per-invoice pipeline independently for each page.

    For a non-PDF image, returns a single-element list (page 1 of 1) —
    the "multiple invoices in one file" concept doesn't apply to a
    standalone image.

    Each element is the same shape returned by extract_from_image(), plus:
      - page_number: 1-indexed position of this page in the file
      - total_pages: total page count of the file
      - image_path: the actual single-page image backing this page (the
        rendered PNG for a PDF page, or file_path itself for a standalone
        image) — always a real, static image, never a multi-page PDF.
        Callers needing to re-look at "the picture for this specific
        invoice" (e.g. ai/vision_verifier.py) should use this instead of
        the original file_path, which for a multi-page PDF doesn't
        identify any single page.
    """
    ext = Path(file_path).suffix.lower()

    if ext != ".pdf":
        result = extract_from_image(
            file_path, use_preprocessing=use_preprocessing, force_handwritten=force_handwritten
        )
        result["page_number"] = 1
        result["total_pages"] = 1
        result["image_path"] = file_path
        return [result]

    image_paths = pdf_to_images(file_path)
    pages = []
    for i, img_path in enumerate(image_paths):
        res = extract_from_image(
            img_path, use_preprocessing=use_preprocessing, force_handwritten=force_handwritten
        )
        res["page_number"] = i + 1
        res["total_pages"] = len(image_paths)
        res["image_path"] = img_path
        pages.append(res)
    return pages
