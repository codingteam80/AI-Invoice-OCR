"""Read small printed fields without rotating individual digit crops.

The original page coordinate system is retained for layout reconciliation.
"""
import re
from pathlib import Path
import cv2
from ocr import paddleocr_engine


def recover_detail_lines(image_path, lines):
    image = cv2.imread(str(image_path))
    if image is None or not lines:
        return lines
    result = [dict(line) for line in lines]
    height, width = image.shape[:2]
    # Re-read compact values and identities with a border. Angle classification
    # of isolated digits can flip 99.00 into 00.66, so it is explicitly disabled.
    for line in result:
        text = str(line.get('text') or '').strip()
        if not (re.fullmatch(r'[()$Pphp\d,.:\s\'-]{1,22}', text) or
                re.search(r'(?i)(?:VAT\s*REG\s*TIN|PLATE\s*NO)', text)):
            continue
        box = line.get('bbox')
        if not box:
            continue
        xs, ys = [p[0] for p in box], [p[1] for p in box]
        x0,y0,x1,y1=max(0,int(min(xs))-2),max(0,int(min(ys))-2),min(width,int(max(xs))+3),min(height,int(max(ys))+3)
        crop=image[y0:y1,x0:x1]
        if not crop.size:
            continue
        crop=cv2.resize(crop,None,fx=2,fy=2,interpolation=cv2.INTER_CUBIC)
        crop=cv2.copyMakeBorder(crop,8,8,8,8,cv2.BORDER_CONSTANT,value=(255,255,255))
        read=paddleocr_engine.recognize_crop(crop,classify_angle=False)
        from ai.source_evidence import number
        numeric_old = number(text)
        numeric_new = number(read.get('text',''))
        identity = bool(re.search(r'(?i)TIN|PLATE', text))
        safe_change = identity or (numeric_old is None and numeric_new is not None)
        if safe_change and read.get('confidence',0)>=0.85 and read.get('text'):
            line['text']=read['text'];line['confidence']=read['confidence']
    return result

def recover_regions(image_path, text):
    """Re-detect narrow header/buyer blocks at their native detail scale."""
    import numpy as np
    image=cv2.imread(str(image_path))
    if image is None:return {}
    h,w=image.shape[:2];regions={}
    boxes={}
    if re.search(r'(?i)parking\s*fee',text):boxes['header']=(0,0,w,int(h*.28))
    if re.search(r'(?i)BILLED TO',text) and re.search(r'(?i)SERVICE INVOICE',text):boxes['buyer']=(int(w*.025),int(h*.18),int(w*.67),int(h*.29))
    ocr=paddleocr_engine._get_paddle_instance() if boxes else None
    for name,(x0,y0,x1,y1) in boxes.items():
        crop=image[y0:y1,x0:x1]
        scale=max(1,min(3,1100/max(1,crop.shape[1])))
        crop=cv2.resize(crop,None,fx=scale,fy=scale,interpolation=cv2.INTER_CUBIC)
        result=ocr.ocr(crop,cls=False)
        rows=[]
        for entry in (result[0] if result else []) or []:
            if entry[1][1]>=.65:rows.append((min(p[1] for p in entry[0]),entry[1][0]))
        regions[name]='\n'.join(t for y,t in sorted(rows))
    return regions
