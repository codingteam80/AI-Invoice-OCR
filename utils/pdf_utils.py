"""PDF handling utilities."""
from pathlib import Path
import fitz  # PyMuPDF
from config.settings import settings
from config.logging import get_logger

logger = get_logger("utils.pdf")


def pdf_to_images(pdf_path: str, dpi: int = 300) -> list[str]:
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
            pix = page.get_pixmap(matrix=matrix)
            out_path = Path(settings.TEMP_DIR) / f"{stem}_page{i+1}.png"
            pix.save(out_path)
            out_paths.append(str(out_path))

    logger.info(f"Converted {pdf_path} -> {len(out_paths)} page image(s)")
    return out_paths


def is_pdf(file_path: str) -> bool:
    return Path(file_path).suffix.lower() == ".pdf"
