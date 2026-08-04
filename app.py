"""
Main application entrypoint.

Usage:
    python app.py ui                 # launch the Streamlit web UI (default)
    python app.py api                # launch the FastAPI REST API (uvicorn)
    python app.py process <file>     # process a single invoice from the CLI
"""
import sys
import subprocess
from pathlib import Path

from config.logging import get_logger
from database.database import init_db

logger = get_logger("app")


def run_ui():
    init_db()
    ui_path = Path(__file__).parent / "ui" / "streamlit_app.py"
    subprocess.run([sys.executable, "-m", "streamlit", "run", str(ui_path)])


def run_api():
    init_db()
    import uvicorn
    uvicorn.run("api.routes:app", host="0.0.0.0", port=8000, reload=True)


def run_process(file_path: str):
    init_db()
    from services.invoice_service import process_invoice_file
    result = process_invoice_file(file_path)
    import json
    print(json.dumps(result, indent=2, default=str))


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
