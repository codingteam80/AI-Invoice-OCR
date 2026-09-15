"""
Central configuration for AI-Invoice-OCR.
Loads values from .env (falls back to sane defaults).
"""
import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent


class Settings:
    # OCR
    # Printed invoices always use PaddleOCR; handwritten invoices are
    # auto-detected and routed to TrOCR. See ocr/ocr_engine.py.
    OCR_LANG: str = os.getenv("OCR_LANG", "en")
    TROCR_MODEL_NAME: str = os.getenv("TROCR_MODEL_NAME", "microsoft/trocr-base-handwritten")

    # PaddleOCR's detector downsizes the image before finding text regions
    # (default det_limit_side_len=960px on the longest side). A dense,
    # full-page multi-item invoice (e.g. PC Worth's 9-row parts list) can
    # lose whole lines of small print once shrunk that far.
    #
    # REVERTED to the library default (960, via None below) after testing
    # against a real 10-invoice batch showed raising this globally to 2400
    # is a net regression: it did fix PC Worth's missing item rows, but it
    # also changed box detection/segmentation on 6 of the other 9 receipts
    # in ways that broke previously-correct extractions — e.g. a
    # crossed-out header line that was correctly ignored before started
    # getting picked up as a phantom line item (Datablitz1), a barcode
    # fused directly onto its item description with no separating space
    # where before OCR kept them apart (Datablitz3), and a "2 @ P399.75"
    # quantity line got split differently and lost its quantity (ACE
    # Hardware). Net effect across the batch: 6 regressions to fix 1
    # document. A global resolution bump isn't the right lever — it needs
    # to be applied selectively (e.g. only when initial detection at
    # default resolution looks sparse relative to image size/density),
    # which is a real feature to design and test properly, not a one-line
    # settings change. Left here, defaulted OFF (None = library default),
    # as an opt-in override for anyone who wants to test raising it
    # against their own specific document set.
    OCR_DET_LIMIT_SIDE_LEN: int | None = (
        int(os.getenv("OCR_DET_LIMIT_SIDE_LEN")) if os.getenv("OCR_DET_LIMIT_SIDE_LEN") else None
    )

    # Handwriting detection (stroke-width-variance heuristic, see
    # ocr/handwriting_detector.py). Higher threshold = harder to classify
    # as handwritten = more documents stay on PaddleOCR.
    HANDWRITING_SWT_THRESHOLD: float = float(os.getenv("HANDWRITING_SWT_THRESHOLD", "0.55"))
    HANDWRITING_MIN_COMPONENTS: int = int(os.getenv("HANDWRITING_MIN_COMPONENTS", "15"))
    # Lower cutoff used when classifying a single cropped line/region rather
    # than a whole page (see ocr/ocr_engine.py hybrid router) — a short
    # invoice line naturally has far fewer glyphs than a full page.
    HANDWRITING_MIN_COMPONENTS_REGION: int = int(os.getenv("HANDWRITING_MIN_COMPONENTS_REGION", "4"))
    # On by default: deskew + denoise + CLAHE contrast enhancement (see
    # ocr/preprocessing.py). This is what actually improves recognition on
    # real phone-photo receipts (faded thermal paper, glare, uneven
    # lighting) before OCR ever runs. Turn off only if you've confirmed it
    # hurts accuracy for your specific document source.
    OCR_PREPROCESS_ENABLED: bool = os.getenv("OCR_PREPROCESS_ENABLED", "false").lower() == "true"
    # Off by default — see ocr/preprocessing.py docstring. Hard binarization
    # tends to hurt PaddleOCR/TrOCR on real phone photos more than it helps.
    OCR_PREPROCESS_BINARIZE: bool = os.getenv("OCR_PREPROCESS_BINARIZE", "false").lower() == "true"

    # On by default: detects a full 90/180/270-degree page rotation (a
    # photo/scan taken sideways or upside-down) and corrects it BEFORE OCR
    # runs — see ocr/ocr_engine.py::correct_page_orientation. This is
    # different from (and runs before) deskew() in ocr/preprocessing.py,
    # which only fixes a few degrees of camera tilt on an already-upright
    # page. Costs up to 4 cheap detection-only PaddleOCR passes per page;
    # turn off only for a document source that's guaranteed to always
    # already be upright (e.g. a scanner with a fixed feed orientation).
    OCR_AUTO_ORIENT_ENABLED: bool = os.getenv("OCR_AUTO_ORIENT_ENABLED", "true").lower() == "true"

    # LLM / Ollama
    OLLAMA_HOST: str = os.getenv("OLLAMA_HOST", "http://localhost:11434")
    OLLAMA_MODEL: str = os.getenv("OLLAMA_MODEL", "qwen2.5:7b")
    LLM_TEMPERATURE: float = float(os.getenv("LLM_TEMPERATURE", "0.1"))
    LLM_MAX_RETRIES: int = int(os.getenv("LLM_MAX_RETRIES", "3"))
    # Generous default: a local CPU-run 7B model on a multi-page PDF's
    # full OCR text can genuinely take longer than 120s to generate a
    # full JSON response. Raise further if you're still seeing timeouts
    # on large documents.
    OLLAMA_TIMEOUT_SECONDS: int = int(os.getenv("OLLAMA_TIMEOUT_SECONDS", "300"))
    # How many times to retry ONLY on a connection/read timeout (distinct
    # from LLM_MAX_RETRIES, which is for validation failures on a JSON
    # response the model DID return in time).
    OLLAMA_TIMEOUT_RETRIES: int = int(os.getenv("OLLAMA_TIMEOUT_RETRIES", "1"))
    # Tells Ollama how long to keep the model loaded in memory after a
    # request. Without this, the model may get unloaded between invoices
    # and the next request pays a cold-start reload on top of generation
    # time — often the real cause of a timeout that "shouldn't" happen.
    OLLAMA_KEEP_ALIVE: str = os.getenv("OLLAMA_KEEP_ALIVE", "30m")

    # Vision cross-check (see ai/vision_verifier.py). Independent second
    # read of the source IMAGE itself (not the OCR text) for the fields
    # where a misread is costliest — lets the pipeline catch OCR errors
    # that the text-only LLM step has no way to see. Off switch included
    # since this roughly doubles per-invoice processing time and needs a
    # vision-capable model actually pulled in Ollama (`ollama pull
    # qwen2.5vl:7b`) — leave disabled until that's done, or on machines
    # too limited to run a second, heavier model.
    VISION_VERIFICATION_ENABLED: bool = os.getenv("VISION_VERIFICATION_ENABLED", "false").lower() == "true"
    VISION_MODEL: str = os.getenv("VISION_MODEL", "qwen2.5vl:7b")
    VISION_TIMEOUT_SECONDS: int = int(os.getenv("VISION_TIMEOUT_SECONDS", "180"))
    VISION_NUM_CTX: int = int(os.getenv("VISION_NUM_CTX", "8192"))

    # 1.52: generic high-resolution vendor-header recovery. This runs only
    # when vendor_address/vendor_tax_id are missing or suspicious after the
    # normal whole-page pass. The crop is intentionally generic (top portion
    # of the page), not tied to any vendor template.
    VENDOR_HEADER_RECOVERY_ENABLED: bool = os.getenv("VENDOR_HEADER_RECOVERY_ENABLED", "true").lower() == "true"
    VENDOR_HEADER_CROP_RATIO: float = float(os.getenv("VENDOR_HEADER_CROP_RATIO", "0.28"))
    VENDOR_HEADER_UPSCALE: float = float(os.getenv("VENDOR_HEADER_UPSCALE", "3.0"))

    # 1.54: dedicated high-resolution image crop for handwritten/garbled invoice dates.
    HANDWRITTEN_DATE_VERIFY_ENABLED: bool = os.getenv("HANDWRITTEN_DATE_VERIFY_ENABLED", "true").lower() == "true"
    # 1.55: dedicated crops for dense statement-summary and customer-address regions.
    STATEMENT_SUMMARY_VERIFY_ENABLED: bool = os.getenv("STATEMENT_SUMMARY_VERIFY_ENABLED", "true").lower() == "true"
    CUSTOMER_ADDRESS_VERIFY_ENABLED: bool = os.getenv("CUSTOMER_ADDRESS_VERIFY_ENABLED", "true").lower() == "true"
    # 1.60: focused final-field crops for ambiguous vendor-header text and vehicle plates.
    VENDOR_ADDRESS_VERIFY_ENABLED: bool = os.getenv("VENDOR_ADDRESS_VERIFY_ENABLED", "true").lower() == "true"
    PLATE_NUMBER_VERIFY_ENABLED: bool = os.getenv("PLATE_NUMBER_VERIFY_ENABLED", "true").lower() == "true"

    # Database
    DATABASE_URL: str = os.getenv("DATABASE_URL", f"sqlite:///{BASE_DIR}/data/invoices.db")

    # Storage paths
    UPLOAD_DIR: Path = Path(os.getenv("UPLOAD_DIR", BASE_DIR / "data" / "uploads"))
    PROCESSED_DIR: Path = Path(os.getenv("PROCESSED_DIR", BASE_DIR / "data" / "processed"))
    TEMP_DIR: Path = Path(os.getenv("TEMP_DIR", BASE_DIR / "data" / "temp"))
    # Auto-cropped/perspective-corrected "scanned" copies from
    # ocr/preprocessing.py::camscan() — see services/invoice_service.py.
    # Separate from PROCESSED_DIR (which holds the original upload) so a
    # person can compare the two, and separate from TEMP_DIR since these
    # are meant to persist and be shown in History, not get cleaned up.
    ENHANCED_DIR: Path = Path(os.getenv("ENHANCED_DIR", BASE_DIR / "data" / "enhanced"))
    EXPORT_DIR: Path = Path(os.getenv("EXPORT_DIR", BASE_DIR / "storage" / "exports"))
    JSON_DIR: Path = BASE_DIR / "storage" / "json"
    DEBUG_DIR: Path = Path(os.getenv("DEBUG_DIR", BASE_DIR / "storage" / "debug"))
    # Keep a complete per-invoice trace (OCR regions + LLM/vision responses +
    # reconciliation/validation). This is intentionally on by default for
    # development/audit builds; set false in production if storage is a concern.
    DIAGNOSTIC_TRACE_ENABLED: bool = os.getenv("DIAGNOSTIC_TRACE_ENABLED", "true").lower() == "true"

    # App behaviour
    APP_ENV: str = os.getenv("APP_ENV", "development")
    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")
    MAX_UPLOAD_MB: int = int(os.getenv("MAX_UPLOAD_MB", "15"))
    CONFIDENCE_THRESHOLD: float = float(os.getenv("CONFIDENCE_THRESHOLD", "0.75"))
    # Fallback used only when the LLM couldn't determine a currency at all
    # (see ai/post_processing.sanitize_for_model). Set this to match where
    # your invoices actually come from — e.g. PHP if you're processing
    # Philippine receipts, since "USD" would silently be wrong otherwise.
    DEFAULT_CURRENCY: str = os.getenv("DEFAULT_CURRENCY", "USD")
    # New, off-by-default: run every uploaded photo (not PDFs) through
    # ocr/preprocessing.py::camscan() — auto-crop to the receipt's edges,
    # straighten, enhance contrast — and save the result for History to
    # display, CamScanner-style. Purely cosmetic/for-the-person's-benefit;
    # it does NOT feed into OCR itself (OCR runs its own, separate
    # preprocessing pass — see OCR_PREPROCESS_ENABLED above). Defaults to
    # False so it's an explicit opt-in, not a silent change to existing
    # upload behavior.
    ENHANCE_IMAGE_ENABLED: bool = os.getenv("ENHANCE_IMAGE_ENABLED", "false").lower() == "true"

    @classmethod
    def ensure_dirs(cls):
        for d in [cls.UPLOAD_DIR, cls.PROCESSED_DIR, cls.TEMP_DIR, cls.ENHANCED_DIR, cls.EXPORT_DIR, cls.JSON_DIR, cls.DEBUG_DIR]:
            Path(d).mkdir(parents=True, exist_ok=True)


settings = Settings()
settings.ensure_dirs()
