"""Local Windows scanning through the free NAPS2 console (no cloud service)."""
import os
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path
from uuid import uuid4

_SCAN_LOCK = threading.Lock()
DRIVERS = {'twain', 'wia', 'escl'}


class ScannerError(RuntimeError):
    pass


def find_console() -> str:
    if os.name != 'nt':
        raise ScannerError('Direct scanning currently requires Windows on the PC running this app.')
    configured = os.environ.get('NAPS2_CONSOLE_PATH', '').strip().strip('"')
    candidates = [configured] if configured else [
        shutil.which('NAPS2.Console.exe'),
        str(Path(os.environ.get('ProgramFiles', r'C:\Program Files')) / 'NAPS2/NAPS2.Console.exe'),
        str(Path(os.environ.get('LOCALAPPDATA', '')) / 'Programs/NAPS2/NAPS2.Console.exe'),
        str(Path(os.environ.get('ProgramFiles(x86)', r'C:\Program Files (x86)')) / 'NAPS2/NAPS2.Console.exe'),
    ]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return str(Path(candidate).resolve())
    raise ScannerError('Install NAPS2 from https://www.naps2.com/download. For a portable/custom installation, set NAPS2_CONSOLE_PATH in .env to the full path of NAPS2.Console.exe and restart the app.')


def _run(args: list[str], timeout: int) -> str:
    if not _SCAN_LOCK.acquire(blocking=False):
        raise ScannerError('The scanner is busy with another request. Please wait and try again.')
    try:
        process = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            try:
                if os.name == 'nt':
                    subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'], capture_output=True, timeout=15)
            except (OSError, subprocess.TimeoutExpired):
                pass
            finally:
                if process.poll() is None:
                    process.kill()
            process.communicate()
            raise ScannerError('Scanning timed out. Check the scanner connection and driver, then try again.')
        output = stdout.decode('utf-8-sig', errors='replace').strip()
        error = stderr.decode('utf-8-sig', errors='replace').strip()
        if process.returncode:
            raise ScannerError('Scanner operation failed. ' + (error or output or 'Check the device connection.')[:1200])
        return output
    except OSError as exc:
        raise ScannerError(f'Could not start the scanning utility: {exc}') from exc
    finally:
        _SCAN_LOCK.release()


def list_scanners(driver: str) -> list[str]:
    if driver not in DRIVERS:
        raise ScannerError('Unsupported scanner driver.')
    output = _run([find_console(), '--listdevices', '--driver', driver], 45)
    return list(dict.fromkeys(line.strip() for line in output.splitlines() if line.strip()))


def scan_page(device: str, driver: str, dpi: int = 300, color: str = 'color', size: str = 'a4') -> tuple[str, bytes]:
    if driver not in DRIVERS or dpi not in (150, 300, 600) or color not in ('color', 'gray') or size not in ('a4', 'letter'):
        raise ScannerError('Invalid scan settings.')
    if not device or '\n' in device or '\r' in device:
        raise ScannerError('Select a scanner first.')
    with tempfile.TemporaryDirectory(prefix='invoice_scan_') as folder:
        output = Path(folder) / 'page.png'
        _run([find_console(), '--noprofile', '--driver', driver, '--device', device,
              '--source', 'glass', '--dpi', str(dpi), '--bitdepth', color, '--pagesize', size,
              '--disableocr', '-n', '1', '-o', str(output)], 180)
        if not output.is_file() or output.stat().st_size == 0:
            raise ScannerError('No scanned page was returned. Check the scanner and try again.')
        from config.settings import settings
        if output.stat().st_size > settings.MAX_UPLOAD_MB * 1024 * 1024:
            raise ScannerError('The scan exceeds the upload limit. Try a lower resolution.')
        data = output.read_bytes()
        from PIL import Image
        import io
        try:
            with Image.open(io.BytesIO(data)) as image:
                image.verify()
        except Exception as exc:
            raise ScannerError('The scanner returned an unreadable image. Please rescan.') from exc
        return f'scan_{uuid4().hex[:12]}.png', data


def prepare_scans(pages: list[tuple[str, bytes]], combine: bool) -> list[tuple[str, bytes]]:
    if not pages:
        raise ScannerError('Scan at least one page first.')
    if not combine:
        return list(pages)
    import fitz
    with fitz.open() as document:
        for _, data in pages:
            with fitz.open(stream=data, filetype='png') as image:
                pdf = image.convert_to_pdf()
            with fitz.open(stream=pdf, filetype='pdf') as page:
                document.insert_pdf(page)
        return [(f'scanned_invoice_{uuid4().hex[:12]}.pdf', document.tobytes(deflate=True))]
