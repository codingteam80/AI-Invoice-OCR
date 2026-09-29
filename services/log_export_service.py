"""Export exact selected audit records; never edit or remove source logs."""
import csv
import io
from datetime import datetime

COLUMNS = ['Log ID', 'Date & Time (PC local)', 'User', 'Action', 'Type', 'Record ID', 'Details']


def log_export_row(row: dict) -> dict:
    stamp = row.get('date_time')
    return dict(zip(COLUMNS, [row['id'], stamp.strftime('%Y-%m-%d %H:%M:%S %z') if isinstance(stamp, datetime) else str(stamp or ''),
                             row.get('user') or '', row.get('action') or '', row.get('entity_type') or '',
                             row.get('entity_id') if row.get('entity_id') is not None else '', row.get('details') or '']))


def export_logs(rows: list[dict], fmt: str) -> bytes:
    if fmt == "pdf":
        return _export_logs_pdf(rows)
    records = [log_export_row(row) for row in rows]
    if fmt == 'csv':
        stream = io.StringIO(newline='')
        writer = csv.writer(stream)
        writer.writerow(COLUMNS)
        for row in records:
            # CSV readers can execute cells beginning with spreadsheet formulas.
            writer.writerow([("'" + value if isinstance(value, str) and value.lstrip().startswith(('=', '+', '-', '@')) else value) for value in row.values()])
        return stream.getvalue().encode('utf-8-sig')
    if fmt != 'xlsx':
        raise ValueError('Choose csv, xlsx, or pdf.')
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = 'Activity Log'
    sheet.append(COLUMNS)
    for row in records:
        sheet.append(list(row.values()))
        for cell in sheet[sheet.max_row]:
            if isinstance(cell.value, str):
                cell.data_type = 's'
            cell.alignment = Alignment(vertical='top', wrap_text=True)
    for cell in sheet[1]:
        cell.font = Font(bold=True)
    sheet.freeze_panes = 'A2'
    sheet.auto_filter.ref = sheet.dimensions
    for column, width in zip('ABCDEFG', [12, 29, 22, 24, 16, 14, 90]):
        sheet.column_dimensions[column].width = width
    output = io.BytesIO()
    workbook.save(output)
    return output.getvalue()


def _export_logs_pdf(rows: list[dict]) -> bytes:
    from html import escape
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib import colors
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.platypus import SimpleDocTemplate, LongTable, TableStyle, Paragraph, Spacer
    output = io.BytesIO()
    doc = SimpleDocTemplate(output, pagesize=landscape(A4), leftMargin=24, rightMargin=24, topMargin=24, bottomMargin=24)
    styles = getSampleStyleSheet()
    cell = ParagraphStyle("LogCell", fontName="Helvetica", fontSize=8, leading=10)
    header = ParagraphStyle("LogHeader", parent=cell, fontName="Helvetica-Bold", textColor=colors.white)
    def para(value, style=cell):
        return Paragraph(escape(str(value)).replace("\n", "<br/>"), style)
    data = [[para(c, header) for c in COLUMNS]]
    data += [[para(value) for value in log_export_row(row).values()] for row in rows]
    table = LongTable(data, repeatRows=1, splitInRow=1, colWidths=[doc.width*w for w in (.05,.15,.10,.12,.08,.08,.42)])
    table.setStyle(TableStyle([
        ("BACKGROUND", (0,0),(-1,0), colors.HexColor("#1F4E78")),
        ("GRID", (0,0),(-1,-1), .3, colors.lightgrey),
        ("VALIGN", (0,0),(-1,-1), "TOP"),
        ("LEFTPADDING", (0,0),(-1,-1), 4), ("RIGHTPADDING", (0,0),(-1,-1), 4),
        ("TOPPADDING", (0,0),(-1,-1), 5), ("BOTTOMPADDING", (0,0),(-1,-1), 5),
    ]))
    doc.build([Paragraph("Activity Log", styles["Title"]), Paragraph(f"{len(rows)} filtered record(s). Dates and times use the PC's local time.", styles["Normal"]), Spacer(1,12), table])
    return output.getvalue()
