import json
from pathlib import Path
from unittest.mock import patch
import numpy as np
import cv2

from ai.post_processing import reconcile_financial_layout
from ai import vision_verifier
from ocr import ocr_engine


def box(x1,y1,x2,y2):
    return [[x1,y1],[x2,y1],[x2,y2],[x1,y2]]


def test_discount_negative_is_positive_internal_value():
    data={"subtotal":2598.0,"tax_amount":0.0,"discount":None,"zero_rated_sales":0.0,"vat_exempt_sales":0.0,"total_amount":2198.0}
    lines=[
        {"text":"Discounts","bbox":box(10,100,100,120)},
        {"text":"-400.00","bbox":box(130,100,200,120)},
    ]
    out,_=reconcile_financial_layout(data,lines)
    assert out["discount"] == 400.0


def test_competing_label_blocks_cross_column_value():
    data={"discount":None,"vat_exempt_sales":None}
    lines=[
        {"text":"VAT EXEMPT SALES","bbox":box(10,100,150,120)},
        {"text":"Less: Discount","bbox":box(220,100,330,120)},
        {"text":"2,468.00","bbox":box(360,100,430,120)},
    ]
    out,_=reconcile_financial_layout(data,lines)
    assert out["vat_exempt_sales"] == 0.0
    assert out["discount"] == 2468.0


def test_vision_payload_requests_8192_context(monkeypatch):
    captured={}
    class R:
        ok=True; status_code=200
        def json(self): return {"response": '{"mismatches":[]}'}
    def fake_post(url,json,timeout):
        captured.update(json); return R()
    monkeypatch.setattr(vision_verifier.requests,"post",fake_post)
    trace={}
    vision_verifier._post_generate("x","abc",trace)
    assert captured["options"]["num_ctx"] >= 8192


def test_orientation_chooses_readability_winner(tmp_path, monkeypatch):
    img=np.full((80,120,3),255,np.uint8)
    path=str(tmp_path/'x.png'); cv2.imwrite(path,img)
    scores=iter([(10.0,{}),(70.0,{}),(8.0,{}),(9.0,{})])
    monkeypatch.setattr(ocr_engine,"_orientation_score",lambda p: next(scores))
    assert ocr_engine._detect_page_orientation(path)==1
