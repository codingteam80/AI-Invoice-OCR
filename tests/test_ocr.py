import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

cv2 = pytest.importorskip("cv2")
import numpy as np
from ocr.preprocessing import to_grayscale, adaptive_threshold


def test_grayscale_conversion():
    img = np.zeros((10, 10, 3), dtype=np.uint8)
    gray = to_grayscale(img)
    assert gray.shape == (10, 10)


def test_adaptive_threshold_output_binary():
    gray = np.random.randint(0, 255, (50, 50), dtype=np.uint8)
    result = adaptive_threshold(gray)
    assert set(np.unique(result)).issubset({0, 255})
