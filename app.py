"""
Main application entrypoint.

Usage:
    python app.py ui                 # launch the Streamlit web UI (default)
    python app.py api                # launch the FastAPI REST API (uvicorn)
    python app.py process <file>     # process a single invoice from the CLI
"""
import os
import sys
import subprocess
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path

from config.logging import get_logger
from database.database import init_db

logger = get_logger("app")


def _ui_port() -> int:
    """Port from .streamlit/config.toml (falls back to Streamlit's default 8501)."""
    try:
        import tomllib
        cfg = tomllib.loads((Path(__file__).parent / ".streamlit" / "config.toml").read_text(encoding="utf-8"))
        return int(cfg.get("server", {}).get("port", 8501))
    except Exception:
        return 8501


def _open_browser_when_ready(port: int, timeout: int = 90) -> None:
    """Wait until Streamlit answers, then open http://localhost:<port> on this PC.

    Uses 'localhost' on purpose: 0.0.0.0 is only a listen address and can't be opened.
    Set NO_BROWSER=1 to disable.
    """
    url = f"http://localhost:{port}"
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"{url}/_stcore/health", timeout=2):
                webbrowser.open(url)
                return
        except Exception:
            time.sleep(1)


def run_ui():
    init_db()
    ui_path = Path(__file__).parent / "ui" / "streamlit_app.py"
    if not os.getenv("NO_BROWSER"):
        threading.Thread(target=_open_browser_when_ready, args=(_ui_port(),), daemon=True).start()
    subprocess.run([sys.executable, "-m", "streamlit", "run", str(ui_path)])


def run_api():
    init_db()
    import uvicorn
    uvicorn.run("api.routes:app", host="0.0.0.0", port=8000, reload=True)


def run_process(file_path: str):
    init_db()
    from services.invoice_service import process_invoice_file
    results = process_invoice_file(file_path)
    import json
    print(json.dumps(results, indent=2, default=str))


def main():
    args = sys.argv[1:]
    command = args[0] if args else "ui"

    if command == "ui":
        run_ui()
    elif command == "api":
        run_api()
    elif command == "process":
        if len(args) < 2:
            print("Usage: python app.py process <file_path>")
            sys.exit(1)
        run_process(args[1])
    else:
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
