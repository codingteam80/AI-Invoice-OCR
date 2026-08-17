"""Image preprocessing to improve OCR accuracy (deskew, denoise, threshold)."""
import cv2
import numpy as np
from config.logging import get_logger

logger = get_logger("ocr.preprocessing")


def load_image(path: str) -> np.ndarray:
    img = cv2.imread(path)
    if img is None:
        raise ValueError(f"Could not read image: {path}")
    return img


def to_grayscale(img: np.ndarray) -> np.ndarray:
    return cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)


def denoise(img: np.ndarray) -> np.ndarray:
    return cv2.fastNlMeansDenoising(img, h=10)


def adaptive_threshold(img: np.ndarray) -> np.ndarray:
    return cv2.adaptiveThreshold(
        img, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 31, 11
    )


def enhance_contrast(img: np.ndarray) -> np.ndarray:
    """CLAHE (Contrast Limited Adaptive Histogram Equalization) on a
    grayscale image.

    Unlike a global histogram-equalization/contrast stretch, CLAHE works on
    small local tiles, so it boosts faded/low-contrast text (the common
    complaint on phone photos of receipts — thermal-paper fade, glare on
    one half of the page, a shadow across a corner) without blowing out
    the parts of the image that were already well-lit. clipLimit caps how
    much any one tile can be stretched, which keeps noise in flat regions
    (blank paper) from getting amplified into speckle.
    """
    clahe = cv2.createCLAHE(clipLimit=2.5, tileGridSize=(8, 8))
    return clahe.apply(img)


def deskew(img: np.ndarray) -> np.ndarray:
    """Estimate skew angle from text contours and rotate to correct it."""
    gray = img if len(img.shape) == 2 else to_grayscale(img)
    inverted = cv2.bitwise_not(gray)
    thresh = cv2.threshold(inverted, 0, 255, cv2.THRESH_BINARY | cv2.THRESH_OTSU)[1]
    coords = np.column_stack(np.where(thresh > 0))
    if coords.shape[0] < 20:
        return img
    angle = cv2.minAreaRect(coords)[-1]
    angle = -(90 + angle) if angle < -45 else -angle
    if abs(angle) < 0.3:
        return img
    (h, w) = img.shape[:2]
    center = (w // 2, h // 2)
    M = cv2.getRotationMatrix2D(center, angle, 1.0)
    rotated = cv2.warpAffine(img, M, (w, h), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REPLICATE)
    logger.debug(f"Deskewed image by {angle:.2f} degrees")
    return rotated


def preprocess(
    path: str,
    save_path: str | None = None,
    binarize: bool = False,
    enhance: bool = True,
) -> np.ndarray:
    """
    Full pipeline: load -> deskew -> grayscale -> denoise -> (optional)
    contrast enhancement -> (optional) threshold.

    binarize=False (the default) is what PaddleOCR/TrOCR should get. Both
    are deep-learning models trained on natural photographs — they expect
    gradients, anti-aliasing, and lighting variation, not a hard black/white
    image. adaptive_threshold() is a classic trick for boosting legacy
    engines like Tesseract, but on real phone photos (glare, shadows,
    uneven lighting, non-white background — exactly what shows up in
    receipts shot on a table) it destroys texture the model relies on and
    tends to make recognition WORSE, not better.

    Only set binarize=True if you've empirically confirmed it helps for
    your specific document source (e.g. clean flatbed scans).

    enhance=True (the default) runs enhance_contrast() (CLAHE) after
    denoising — this is what actually helps the common "photo of a faded
    receipt" case, without the aggressive black/white flattening that
    binarize does. Unlike binarize, it's safe to leave on by default since
    it preserves the gradient information PaddleOCR/TrOCR expect.
    """
    img = load_image(path)
    img = deskew(img)
    gray = to_grayscale(img)
    gray = denoise(gray)
    if enhance:
        gray = enhance_contrast(gray)
    result = adaptive_threshold(gray) if binarize else gray
    if save_path:
        cv2.imwrite(save_path, result)
    return result
