"""
TrOCR-backed text recognition — used for handwritten regions.

TrOCR (microsoft/trocr-*-handwritten) is a line-level recognizer: it needs
each text line/word already cropped out of the page. It has no built-in
detector, so detection is delegated to paddleocr_engine.detect_boxes()
(PaddleOCR used purely as a line-finder here, not for reading the text).

Two entry points:
  - recognize_crop(pil_image): recognize a single already-cropped region.
    This is what ocr/ocr_engine.py's hybrid per-region router calls.
  - extract_text(image_path): whole-page convenience wrapper (detect all
    boxes, recognize every one with TrOCR) — used when a caller has
    manually pinned a page as entirely handwritten (force_handwritten=True).
"""
from functools import lru_cache

import numpy as np
from PIL import Image

from config.logging import get_logger
from config.settings import settings
from ocr import paddleocr_engine

logger = get_logger("ocr.trocr")


@lru_cache(maxsize=1)
def _get_trocr():
    import torch
    from transformers import TrOCRProcessor, VisionEncoderDecoderModel

    logger.info(f"Loading TrOCR model ({settings.TROCR_MODEL_NAME})...")
    processor = TrOCRProcessor.from_pretrained(settings.TROCR_MODEL_NAME)
    model = VisionEncoderDecoderModel.from_pretrained(settings.TROCR_MODEL_NAME)
    device = "cuda" if torch.cuda.is_available() else "cpu"
    model.to(device)
    model.eval()
    return processor, model, device


def _bbox_to_crop(img: Image.Image, bbox: list) -> Image.Image:
    xs = [p[0] for p in bbox]
    ys = [p[1] for p in bbox]
    left, right = max(0, int(min(xs))), int(max(xs))
    top, bottom = max(0, int(min(ys))), int(max(ys))
    if right <= left or bottom <= top:
        return img
    return img.crop((left, top, right, bottom))


def recognize_crop(crop: Image.Image) -> dict:
    """
    Recognize a single already-cropped line/word image with TrOCR.
    `crop` must be a PIL Image (RGB).

    Returns: {"text": str, "confidence": float}
    """
    import torch

    processor, model, device = _get_trocr()
    pixel_values = processor(images=crop, return_tensors="pt").pixel_values.to(device)

    with torch.no_grad():
        outputs = model.generate(
            pixel_values,
            output_scores=True,
            return_dict_in_generate=True,
            max_new_tokens=64,
        )

    text = processor.batch_decode(outputs.sequences, skip_special_tokens=True)[0].strip()

    conf = 1.0
    if outputs.scores:
        probs = [torch.softmax(s, dim=-1).max().item() for s in outputs.scores]
        conf = float(np.mean(probs)) if probs else 1.0

    return {"text": text, "confidence": round(conf, 4)}


def extract_text(image_path: str) -> dict:
    """
    Whole-page TrOCR pass: detect all line boxes (via PaddleOCR detector)
    and recognize every one with TrOCR. Used when a page is manually
    pinned as entirely handwritten (force_handwritten=True) — the default
    auto-routing path uses recognize_crop() per region instead, so printed
    lines on a mixed page still go to PaddleOCR.
    """
    boxes = paddleocr_engine.detect_boxes(image_path)
    if not boxes:
        logger.warning(f"No text regions detected in {image_path} for TrOCR recognition")
        return {"text": "", "lines": [], "avg_confidence": 0.0}

    img = Image.open(image_path).convert("RGB")
    lines, confidences, text_parts = [], [], []

    for bbox in boxes:
        crop = _bbox_to_crop(img, bbox)
        res = recognize_crop(crop)
        if not res["text"]:
            continue
        lines.append({"text": res["text"], "confidence": res["confidence"], "bbox": bbox})
        confidences.append(res["confidence"])
        text_parts.append(res["text"])

    avg_conf = sum(confidences) / len(confidences) if confidences else 0.0
    return {
        "text": "\n".join(text_parts),
        "lines": lines,
        "avg_confidence": round(avg_conf, 4),
    }
