"""Targeted regressions for browser sessions and the requested receipt fields."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pytest
from ai.evidence_corrections import clear_blank_party_fields, keep_first_vendor_address
from ai.post_processing import reconcile_parking_tax_summary


def test_blank_printed_fields_and_real_values():
    got, _ = clear_blank_party_fields({'customer_name': 'NAME :________________', 'customer_tax_id': 'TIN :_______'})
    assert got == {'customer_name': None, 'customer_tax_id': None}
    real = {'customer_name': 'Virben De la Vega', 'customer_tax_id': '123-456-789', 'customer_address': '12 Name Street'}
    assert clear_blank_party_fields(real)[0] == real


def test_first_vendor_location():
    first = '32nd Street corner 7th Avenue, Bonifacio Global City, Taguig, Philippines 1634'
    second = '9/F The Globe Tower-Cebu, Samar Loop cor Panay Road, Cebu Business Park, Cebu City 6000'
    for sep in [' ', '\n', '; ']:
        assert keep_first_vendor_address({'vendor_address': first + sep + second})[0]['vendor_address'] == first
    assert keep_first_vendor_address({'vendor_address': first})[0]['vendor_address'] == first


def test_parking_rate_not_part_of_amount():
    got, _ = reconcile_parking_tax_summary({'total_amount': 220, 'tax_amount': 1223.57}, 'PARKING FEE\nVAT Amount12%\n:\n23.57\nName\nTIN')
    assert got['tax_amount'] == 23.57
    assert got['subtotal'] == 196.43


def test_no_invented_tax_when_printed_amount_missing():
    data = {'total_amount': 220, 'tax_amount': 1223.57}
    assert reconcile_parking_tax_summary(data, 'PARKING FEE\nVAT Amount12%\nName')[0] == data
    assert reconcile_parking_tax_summary(data, 'Sales invoice\nVAT Amount12% 23.57')[0] == data


def test_browser_token_restore_revoke_and_account_changes(tmp_path, monkeypatch):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from database.database import Base
    from database.models import UserORM, BrowserLoginSessionORM
    from services import browser_session_service as svc
    engine = create_engine('sqlite:///' + str(tmp_path / 'auth.db'))
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    monkeypatch.setattr(svc, 'SessionLocal', factory)
    with factory() as db:
        user = UserORM(username='test', password_hash='hash-one',role='user',is_active=True,must_change_password=False)
        db.add(user); db.commit(); uid = user.id
    token = svc.create_browser_session(uid)
    assert svc.browser_session_user(token)['username'] == 'test'
    with factory() as db:
        assert db.query(BrowserLoginSessionORM).one().token_hash != token
    svc.revoke_browser_session(token)
    assert svc.browser_session_user(token) is None
    token = svc.create_browser_session(uid)
    with factory() as db:
        db.get(UserORM, uid).password_hash = 'hash-two'; db.commit()
    assert svc.browser_session_user(token) is None
    token = svc.create_browser_session(uid)
    with factory() as db:
        db.get(UserORM, uid).is_active = False; db.commit()
    assert svc.browser_session_user(token) is None
    assert svc.browser_session_user('invalid-token') is None
