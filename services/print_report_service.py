"""Print the existing PDF export layout instead of maintaining a second report."""
import base64
import hashlib
import html
import json
import tempfile
from pathlib import Path


def report_signature(records: list[dict], chart_path: str | None = None) -> str:
    digest = hashlib.sha256(json.dumps(records, sort_keys=True, default=str).encode())
    if chart_path and Path(chart_path).is_file():
        digest.update(Path(chart_path).read_bytes())
    return digest.hexdigest()


def build_report_pdf(invoices: list[dict], chart_path: str | None = None) -> bytes:
    from exports.pdf_export import export_to_pdf
    # Use exactly the Generate Export PDF renderer, including its chart page.
    with tempfile.TemporaryDirectory(prefix='invoice_report_') as folder:
        path = Path(folder) / 'invoices_report.pdf'
        export_to_pdf(invoices, filename=str(path), chart_path=chart_path)
        return path.read_bytes()


def pdf_print_html(pdf: bytes, title: str = 'Invoice Report') -> str:
    """Vector page previews derived from PDF, printable in an isolated iframe.

    The PDF remains available as a direct download. Using SVG avoids blurry
    screenshots and browser PDF-plugin restrictions inside sandboxed iframes.
    """
    import fitz
    with fitz.open(stream=pdf, filetype='pdf') as document:
        if not len(document):
            raise ValueError('There are no pages to print.')
        width = document[0].rect.width * 25.4 / 72
        height = document[0].rect.height * 25.4 / 72
        pages = []
        for index, page in enumerate(document):
            svg = page.get_svg_image(text_as_path=True).encode()
            src = 'data:image/svg+xml;base64,' + base64.b64encode(svg).decode()
            pages.append(f'<div class="page"><img alt="Page {index+1}" src="{src}"></div>')
    return f'''<!doctype html><html><head><meta charset="utf-8"><title>{html.escape(title)}</title>
<style>
html,body{{margin:0;padding:0;background:#e9edf0;font-family:Arial,sans-serif}}
.toolbar{{padding:12px;background:white;position:sticky;top:0;border-bottom:1px solid #ccc}}
button{{padding:12px 20px;background:#1f4e78;color:white;border:0;border-radius:4px;font-size:16px;cursor:pointer}}
.page{{background:white;margin:12px auto;width:100%;max-width:{width:.3f}mm;line-height:0}}
.page img{{display:block;width:100%;height:auto}}
@page{{size:{width:.3f}mm {height:.3f}mm;margin:0}}
@media print{{
html,body{{background:white;margin:0!important;padding:0!important;print-color-adjust:exact;-webkit-print-color-adjust:exact}}
.toolbar{{display:none!important}}
.page{{margin:0!important;width:100%;max-width:none;height:{height-.5:.3f}mm;break-inside:avoid;break-after:page;page-break-after:always}}
.page:last-child{{break-after:auto;page-break-after:auto}}
.page img{{width:100%;height:100%;object-fit:contain}}
}}
</style></head><body>
<div class="toolbar"><button id="print" onclick="printPages()">Print / choose printer</button>
<span id="message">Use landscape and turn off browser headers and footers. The source PDF is also available for download.</span></div>
{''.join(pages)}
<script>
async function printPages() {{
 const button=document.getElementById('print');button.disabled=true;
 try {{
  await Promise.all(Array.from(document.images).map(img => img.decode()));
  window.focus();window.print();
 }} catch(error) {{document.getElementById('message').textContent='Could not open printing. Download the source PDF and print from your PDF viewer.';}}
 finally {{button.disabled=false;}}
}}
</script></body></html>'''


def build_print_report(invoices: list[dict], chart_path: str | None = None) -> str:
    return pdf_print_html(build_report_pdf(invoices, chart_path))
