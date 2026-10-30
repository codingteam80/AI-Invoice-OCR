# Single-host server setup (English)

One Windows PC hosts Streamlit, the SQLite invoice database, and optional Ollama. Other
computers on the same trusted office network may open the server in a browser. The OCR
and model processing run on the **host**, not the browser/client PC.

## Before starting

1. Install Python and packages using `python -m pip install -r requirements.txt` in a virtual environment.
2. Copy `.env.example` to `.env`; configure `LLM_API_KEY` for Groq or select local
   Ollama. The default Groq API uses `https://api.groq.com/openai/v1` and
   `qwen/qwen3.8-27b`. The local option needs Ollama installed and its model pulled.
3. Back up the existing `.env`, `data/` (including `invoices.db`), and `storage/`
   before upgrading. Source ZIPs should not overwrite your production database.
4. Launch `python app.py ui` on the host to verify the UI locally at
   `http://localhost:8501`.

## Trusted local network use

1. On the host PC, run `open_firewall.bat` as administrator only if you want other
   PCs on the same trusted/private network to connect. This opens Streamlit port 8501.
2. Start the host using `start_server.bat`, leaving its terminal open. Use the host
   machine's LAN IPv4 address (find it with `ipconfig`).
3. On another PC open `http://HOST-LAN-IP:8501` and sign in using an individual app account.
4. Do not browse to `http://0.0.0.0:8501`: it is a binding address, not a client URL.
5. Ensure the host doesn't sleep while others are working. Stopping or restarting the
   server invalidates login sessions; a new sign-in is required.

## Security and operating limits

- HTTP on a LAN is **not encrypted**. Do not expose port 8501 directly to the internet;
  use a properly configured reverse proxy with TLS and access controls if remote access
  is required.
- Initial administrator defaults may be defined by `DEFAULT_ADMIN_USERNAME` and
  `DEFAULT_ADMIN_PASSWORD`; change them promptly and use individual accounts.
- The FastAPI endpoints launched with `python app.py api` do **not** inherit Streamlit's
  account authentication. Do not expose port 8000 to untrusted networks.
- Groq API mode sends invoice data to the remote provider; Ollama-only mode processes
  locally, provided you disable remote fallback/provider selection.
- Upload only **one distinct invoice per file**; several pages of the same invoice
  may be combined. Scan one invoice at a time.
- Database/processed-image backups should be taken regularly. Simultaneous local OCR
  uploads can slow the shared host.
