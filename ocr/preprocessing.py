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


def rotate_90(img: np.ndarray, quarter_turns: int) -> np.ndarray:
    """
    Rotate `img` by a multiple of 90 degrees, clockwise, `quarter_turns`
    times (0-3). Lossless and interpolation-free (unlike deskew()'s
    warpAffine, which is for small sub-degree corrections) — this is for
    correcting a document that's a full 90/180/270 off, e.g. a portrait
    invoice photographed/scanned sideways in landscape orientation. See
    ocr/ocr_engine.py::_detect_page_orientation, which tries all 4
    quarter-turns and picks whichever one PaddleOCR's text detector finds
    the most text in.
    """
    quarter_turns %= 4
    if quarter_turns == 0:
        return img
    rotate_code = {
        1: cv2.ROTATE_90_CLOCKWISE,
        2: cv2.ROTATE_180,
        3: cv2.ROTATE_90_COUNTERCLOCKWISE,
    }[quarter_turns]
    return cv2.rotate(img, rotate_code)


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
    # deskew is only for small camera/scanner tilt. Full 90/180/270 page
    # orientation is handled separately in ocr/ocr_engine.py. minAreaRect
    # can occasionally report a large angle for an already-upright page;
    # applying that here rotates a valid invoice sideways.
    if abs(angle) > 10.0:
        logger.warning(f"Deskew estimated {angle:.2f} degrees; skipping because it exceeds the 10-degree safe limit")
        return img
    (h, w) = img.shape[:2]
    center = (w // 2, h // 2)
    M = cv2.getRotationMatrix2D(center, angle, 1.0)
    rotated = cv2.warpAffine(img, M, (w, h), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REPLICATE)
    logger.debug(f"Deskewed image by {angle:.2f} degrees")
    return rotated


def order_points(pts: np.ndarray) -> np.ndarray:
    """Sort 4 corner points into [top-left, top-right, bottom-right, bottom-left]
    order, regardless of what order find_document_contour() found them in.
    Standard trick: top-left has the smallest x+y sum, bottom-right the
    largest; top-right has the smallest x-y difference, bottom-left the
    largest."""
    rect = np.zeros((4, 2), dtype="float32")
    s = pts.sum(axis=1)
    rect[0] = pts[np.argmin(s)]
    rect[2] = pts[np.argmax(s)]
    diff = np.diff(pts, axis=1)
    rect[1] = pts[np.argmin(diff)]
    rect[3] = pts[np.argmax(diff)]
    return rect


def find_document_contour(img: np.ndarray) -> np.ndarray | None:
    """Find the 4-corner outline of a document/receipt against its
    background. Returns 4 (x, y) points, or None if nothing convincingly
    document-shaped was found (e.g. the receipt already fills the whole
    frame edge-to-edge, or the background doesn't contrast with the paper)
    — callers must treat None as "don't crop", not as an error, since a
    wrong crop that cuts off real text is worse than no crop at all.
    """
    h, w = img.shape[:2]
    # Work on a downscaled copy for speed/stability — contour detection on
    # a full-resolution phone photo is slow and noisier (more small edge
    # fragments from paper texture/print); scale back up at the end.
    scale = 700.0 / max(h, w)
    small = cv2.resize(img, (int(w * scale), int(h * scale))) if scale < 1 else img.copy()

    gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
    blurred = cv2.GaussianBlur(gray, (5, 5), 0)
    edges = cv2.Canny(blurred, 50, 150)
    edges = cv2.dilate(edges, np.ones((5, 5), np.uint8), iterations=1)

    contours, _ = cv2.findContours(edges, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return None

    small_area = small.shape[0] * small.shape[1]
    best = None
    for c in sorted(contours, key=cv2.contourArea, reverse=True)[:10]:
        area = cv2.contourArea(c)
        # A crop candidate must be a sizeable fraction of the frame (skip
        # small noise contours) but not the ENTIRE frame (skip the image
        # border itself, which Canny often outlines as a contour).
        if area < 0.2 * small_area or area > 0.98 * small_area:
            continue
        peri = cv2.arcLength(c, True)
        approx = cv2.approxPolyDP(c, 0.02 * peri, True)
        if len(approx) == 4 and cv2.isContourConvex(approx):
            best = approx
            break

    if best is None:
        return None

    pts = best.reshape(4, 2).astype("float32") / scale  # back to original resolution
    return order_points(pts)


def four_point_transform(img: np.ndarray, pts: np.ndarray) -> np.ndarray:
    """Perspective-warp the quadrilateral `pts` (in original-image
    coordinates, [tl, tr, br, bl] order) into a flat, upright rectangle —
    the actual "scan" step: a receipt photographed at an angle, with
    trapezoidal perspective distortion, comes out as a straight top-down
    rectangle."""
    (tl, tr, br, bl) = pts
    width_a = np.linalg.norm(br - bl)
    width_b = np.linalg.norm(tr - tl)
    max_width = max(int(width_a), int(width_b))

    height_a = np.linalg.norm(tr - br)
    height_b = np.linalg.norm(tl - bl)
    max_height = max(int(height_a), int(height_b))

    if max_width < 10 or max_height < 10:
        return img  # degenerate quad, e.g. contour was a sliver — bail out

    dst = np.array([
        [0, 0],
        [max_width - 1, 0],
        [max_width - 1, max_height - 1],
        [0, max_height - 1],
    ], dtype="float32")

    M = cv2.getPerspectiveTransform(pts, dst)
    return cv2.warpPerspective(img, M, (max_width, max_height))


def camscan(
    path: str,
    save_path: str | None = None,
    enhance: bool = True,
) -> np.ndarray:
    """CamScanner-style pipeline: detect the receipt's edges in the photo,
    perspective-correct it to a flat top-down crop, then run the same
    deskew/denoise/contrast steps as preprocess(). Falls back to the
    uncropped, deskewed original if no confident 4-corner document outline
    is found — this is deliberately conservative, since a bad automatic
    crop that slices off part of the receipt is a worse failure mode than
    just not cropping.

    Returns a color (BGR) image, since this is meant to produce something
    a person looks at (saved alongside the original upload, shown in
    History) — unlike preprocess(), which hands PaddleOCR/TrOCR a
    grayscale array it never needs to be color for.
    """
    img = load_image(path)
    corners = find_document_contour(img)
    if corners is not None:
        img = four_point_transform(img, corners)
        logger.debug("camscan: cropped to detected document contour")
    else:
        logger.debug("camscan: no confident document contour found, skipping crop")

    img = deskew(img)
    if enhance:
        gray = to_grayscale(img)
        gray = denoise(gray)
        gray = enhance_contrast(gray)
        # Blend the contrast-enhanced luminance back into the color image
        # rather than discarding color entirely — this keeps it looking
        # like a "scanned document", not a grayscale OCR-intermediate.
        img = cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)

    if save_path:
        cv2.imwrite(save_path, img)
    return img


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
