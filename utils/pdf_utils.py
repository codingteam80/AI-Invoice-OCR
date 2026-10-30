"""PDF handling utilities."""
from pathlib import Path
import fitz  # PyMuPDF
from config.settings import settings
from config.logging import get_logger

logger = get_logger("utils.pdf")


def pdf_to_images(pdf_path: str, dpi: int = 180) -> list[str]:
    """Convert each page of a PDF into a PNG saved in TEMP_DIR; returns file paths.

    Uses PyMuPDF (fitz) instead of pdf2image so this doesn't depend on the
    poppler binary being installed/on PATH (avoids
    "PDFInfoNotInstalledError: Unable to get page count. Is poppler
    installed and in PATH?").
    """
    stem = Path(pdf_path).stem
    out_paths = []
    # PyMuPDF renders at 72 DPI natively, so scale the render matrix to hit
    # the requested dpi.
    zoom = dpi / 72
    matrix = fitz.Matrix(zoom, zoom)

    with fitz.open(pdf_path) as doc:
        for i, page in enumerate(doc):
            scale = min(zoom, (16_000_000 / max(1, page.rect.width * page.rect.height)) ** 0.5)
            pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
            out_path = Path(settings.TEMP_DIR) / f"{stem}_page{i+1}.png"
            pix.save(out_path)
            out_paths.append(str(out_path))

    logger.info(f"Converted {pdf_path} -> {len(out_paths)} page image(s)")
    return out_paths


def is_pdf(file_path: str) -> bool:
    return Path(file_path).suffix.lower() == ".pdf"


def native_pdf_pages(pdf_path: str, image_paths: list[str]) -> list[dict | None]:
    """Use only a usable embedded text layer; scanned/garbled pages still need OCR."""
    results = []
    with fitz.open(pdf_path) as doc:
        for page, image_path in zip(doc, image_paths):
            text = page.get_text("text", sort=True).strip()
            if len(text) < 40 or text.count("\ufffd") > len(text) * 0.01:
                if not text and not page.get_images() and not page.get_drawings():
                    results.append({"text": "", "lines": [], "avg_confidence": 0.0,
                                    "engine": "pdf_text", "blank_page": True})
                else:
                    results.append(None)
                continue
            from PIL import Image
            with Image.open(image_path) as image:
                sx, sy = image.width / page.rect.width, image.height / page.rect.height
            lines = []
            for block in page.get_text("dict", sort=True)["blocks"]:
                for line in block.get("lines", []):
                    value = " ".join(span["text"] for span in line["spans"]).strip()
                    if not value:
                        continue
                    x0, y0, x1, y1 = line["bbox"]
                    lines.append({"text": value, "confidence": 1.0,
                                  "bbox": [[x0*sx,y0*sy],[x1*sx,y0*sy],[x1*sx,y1*sy],[x0*sx,y1*sy]]})
            results.append({"text": text, "lines": lines, "avg_confidence": 1.0,
                            "engine": "pdf_text", "processed_image_path": image_path})
    return results
