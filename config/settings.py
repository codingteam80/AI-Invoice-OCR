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

    # Handwriting detection (stroke-width-variance heuristic, see
    # ocr/handwriting_detector.py). Higher threshold = harder to classify
    # as handwritten = more documents stay on PaddleOCR.
    HANDWRITING_SWT_THRESHOLD: float = float(os.getenv("HANDWRITING_SWT_THRESHOLD", "0.55"))
    HANDWRITING_MIN_COMPONENTS: int = int(os.getenv("HANDWRITING_MIN_COMPONENTS", "15"))
    # Lower cutoff used when classifying a single cropped line/region rather
    # than a whole page (see ocr/ocr_engine.py hybrid router) — a short
    # invoice line naturally has far fewer glyphs than a full page.
    HANDWRITING_MIN_COMPONENTS_REGION: int = int(os.getenv("HANDWRITING_MIN_COMPONENTS_REGION", "4"))
    # Off by default — see ocr/preprocessing.py docstring. Hard binarization
    # tends to hurt PaddleOCR/TrOCR on real phone photos more than it helps.
    OCR_PREPROCESS_BINARIZE: bool = os.getenv("OCR_PREPROCESS_BINARIZE", "false").lower() == "true"

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

    # Database
    DATABASE_URL: str = os.getenv("DATABASE_URL", f"sqlite:///{BASE_DIR}/data/invoices.db")

    # Storage paths
    UPLOAD_DIR: Path = Path(os.getenv("UPLOAD_DIR", BASE_DIR / "data" / "uploads"))
    PROCESSED_DIR: Path = Path(os.getenv("PROCESSED_DIR", BASE_DIR / "data" / "processed"))
    TEMP_DIR: Path = Path(os.getenv("TEMP_DIR", BASE_DIR / "data" / "temp"))
    EXPORT_DIR: Path = Path(os.getenv("EXPORT_DIR", BASE_DIR / "storage" / "exports"))
    JSON_DIR: Path = BASE_DIR / "storage" / "json"

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

    @classmethod
    def ensure_dirs(cls):
        for d in [cls.UPLOAD_DIR, cls.PROCESSED_DIR, cls.TEMP_DIR, cls.EXPORT_DIR, cls.JSON_DIR]:
            Path(d).mkdir(parents=True, exist_ok=True)


settings = Settings()
settings.ensure_dirs()
