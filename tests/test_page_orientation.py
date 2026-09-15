"""
Tests for whole-page 90/180/270-degree orientation auto-correction
(ocr/ocr_engine.py::correct_page_orientation). Confirmed real bug: an SGV
Gorres Velayo invoice scanned in landscape orientation came back at 0%
confidence with every field blank, because neither deskew() (small camera
tilt only) nor PaddleOCR's use_angle_cls (per-line 0<->180 flip only)
handles a page rotated a full 90/270 degrees.

PaddleOCR itself isn't installed/runnable in this test environment, so
paddleocr_engine.detect_boxes is monkeypatched to simulate "found lots of
text at this rotation" vs "found nothing" per candidate orientation,
matching the real-world signal the function relies on.
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import types

# ocr/ocr_engine.py does `import torch` at module load purely to win a DLL-
# loading race against paddlepaddle on Windows (see its own comment) — it
# never actually calls into torch. Stubbing it out here lets these
# orientation-logic tests run without installing the real (very heavy)
# torch/paddleocr/paddlepaddle dependencies, which aren't needed: every
# PaddleOCR call in this test file is monkeypatched anyway.
if "torch" not in sys.modules:
    sys.modules["torch"] = types.ModuleType("torch")

import numpy as np
import cv2

from config.settings import settings
from ocr.preprocessing import rotate_90
from ocr import ocr_engine, paddleocr_engine


def _make_test_image(tmp_path, name="test_invoice.png", shape=(300, 200, 3)):
    """A plain image standing in for 'a portrait invoice, upright'."""
    img = np.full(shape, 255, dtype="uint8")
    path = str(tmp_path / name)
    cv2.imwrite(path, img)
    return path


def test_rotate_90_helper_dimensions():
    img = np.zeros((100, 200, 3), dtype="uint8")  # landscape, 100 tall x 200 wide
    assert rotate_90(img, 0).shape == (100, 200, 3)
    assert rotate_90(img, 1).shape == (200, 100, 3)  # 90 deg -> swaps dimensions
    assert rotate_90(img, 2).shape == (100, 200, 3)  # 180 deg -> same dimensions
    assert rotate_90(img, 3).shape == (200, 100, 3)  # 270 deg -> swaps dimensions
    assert rotate_90(img, 4).shape == (100, 200, 3)  # wraps around (4 == 0)


def test_upright_page_is_not_rotated(tmp_path, monkeypatch):
    image_path = _make_test_image(tmp_path)

    # Simulate: the ORIGINAL (0-degree) file always finds lots of text;
    # every rotated candidate finds none — i.e. the page is already upright.
    def fake_detect_boxes(path):
        if path == image_path:
            return [[[0, 0], [50, 0], [50, 20], [0, 20]]] * 30
        return []

    monkeypatch.setattr(paddleocr_engine, "detect_boxes", fake_detect_boxes)
    monkeypatch.setattr(settings, "OCR_AUTO_ORIENT_ENABLED", True)

    result_path = ocr_engine.correct_page_orientation(image_path)
    assert result_path == image_path  # untouched — no rotation needed


def test_sideways_page_gets_rotated(tmp_path, monkeypatch):
    """The SGV-style bug: text is only detectable once the page is rotated
    90 degrees clockwise — correct_page_orientation should find and apply
    that rotation, producing a NEW file distinct from the original."""
    image_path = _make_test_image(tmp_path)

    call_log = []

    def fake_detect_boxes(path):
        call_log.append(path)
        if path == image_path:
            return []  # sideways: original orientation finds nothing
        if "orient_probe_1_" in path:
            return [[[0, 0], [50, 0], [50, 20], [0, 20]]] * 40  # the right rotation
        return [[[0, 0], [10, 0], [10, 5], [0, 5]]] * 2  # other rotations: barely anything

    monkeypatch.setattr(paddleocr_engine, "detect_boxes", fake_detect_boxes)
    monkeypatch.setattr(settings, "OCR_AUTO_ORIENT_ENABLED", True)

    result_path = ocr_engine.correct_page_orientation(image_path)
    assert result_path != image_path
    assert Path(result_path).exists()
    assert len(call_log) >= 4  # probed all 4 quarter-turns


def test_orientation_correction_disabled_via_setting(tmp_path, monkeypatch):
    image_path = _make_test_image(tmp_path)

    def fake_detect_boxes(path):
        return [] if path == image_path else [[[0, 0], [50, 0], [50, 20], [0, 20]]] * 40

    monkeypatch.setattr(paddleocr_engine, "detect_boxes", fake_detect_boxes)
    monkeypatch.setattr(settings, "OCR_AUTO_ORIENT_ENABLED", False)

    # Even though a rotation would clearly help, the setting being off
    # means correct_page_orientation must not touch the image at all.
    result_path = ocr_engine.correct_page_orientation(image_path)
    assert result_path == image_path
