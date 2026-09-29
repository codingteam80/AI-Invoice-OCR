import base64
import io
import os
from pathlib import Path
import re
import subprocess
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fitz
from services.log_export_service import export_logs
from services.print_report_service import build_report_pdf, pdf_print_html, report_signature


def test_print_reuses_export_renderer(tmp_path):
    from exports.pdf_export import export_to_pdf
    rows = [dict(id=3, invoice_number='INV-3', vendor_name='A & <B>', total_amount=220, tax_amount=23.57, subtotal=196.43, currency='PHP')]
    generated = Path(export_to_pdf(rows, filename=str(tmp_path/'export.pdf'))[0]).read_bytes()
    printed = build_report_pdf(rows)
    with fitz.open(stream=generated, filetype='pdf') as left, fitz.open(stream=printed, filetype='pdf') as right:
        assert len(left) == len(right)
        for p1, p2 in zip(left, right):
            assert p1.rect == p2.rect
            assert p1.get_pixmap().samples == p2.get_pixmap().samples
        assert 'A & <B>' in right[0].get_text()
    markup = pdf_print_html(printed)
    assert '@page{size:420.' in markup
    assert 'window.print()' in markup and 'text-as-path' not in markup
    svg = base64.b64decode(re.search('data:image/svg\\+xml;base64,([^\"]+)', markup)[1])
    assert b'<svg' in svg and b'<path' in svg


def test_log_pdf_has_all_filtered_records_and_can_split_long_details():
    rows = [dict(id=33,user='tester',action='EDIT',details=('Detail line\n'*200)+'END OF FIRST'),dict(id=44,user='admin',action='UPLOAD',details='END OF SECOND')]
    pdf = export_logs(rows, 'pdf')
    with fitz.open(stream=pdf, filetype='pdf') as doc:
        text = ''.join(p.get_text() for p in doc)
        assert 'END OF FIRST' in text and 'END OF SECOND' in text
        assert len(doc)>1
    html = pdf_print_html(pdf, 'Activity Log')
    assert html.count('class="page"') == len(re.findall('data:image/svg',html))


def test_chart_is_in_same_print_pdf(tmp_path):
    from PIL import Image
    path=tmp_path/'chart.png';Image.new('RGB',(200,100),'blue').save(path)
    rows=[dict(id=1,total_amount=1)]
    pdf=build_report_pdf(rows,str(path))
    with fitz.open(stream=pdf,filetype='pdf') as doc:
        assert len(doc)==2 and 'Summary Chart' in doc[1].get_text()
    before=report_signature(rows,str(path))
    Image.new('RGB',(200,100),'red').save(path)
    assert before!=report_signature(rows,str(path))


def test_token_valid_in_process_but_invalid_after_restart(tmp_path):
    env=dict(os.environ, DATABASE_URL='sqlite:///'+str(tmp_path/'auth.db'), PYTHONPATH=str(Path(__file__).resolve().parents[1]))
    create='''from database.database import init_db,SessionLocal
from database.models import UserORM
from services.browser_session_service import create_browser_session,browser_session_user
init_db()
with SessionLocal() as db:
 u=UserORM(username="restart-test",password_hash="test-hash",role="user",is_active=True,must_change_password=False)
 db.add(u);db.commit();uid=u.id
token=create_browser_session(uid)
assert browser_session_user(token)["username"]=="restart-test"
assert browser_session_user(token)["username"]=="restart-test"
print(token)
'''
    run=subprocess.run([sys.executable,'-c',create],env=env,text=True,capture_output=True,check=True)
    token=run.stdout.strip().splitlines()[-1]
    restore='''import sys
from services.browser_session_service import browser_session_user
assert browser_session_user(sys.argv[1]) is None
print("restart rejected old token")
'''
    result=subprocess.run([sys.executable,'-c',restore,token],env=env,text=True,capture_output=True,check=True)
    assert 'restart rejected old token' in result.stdout
