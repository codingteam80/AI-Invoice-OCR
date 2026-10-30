"""Optional, free, offline clarity improvement for a low-resolution invoice photo.

Usage: python tools/enhance_low_resolution_invoice.py input.jpg output.png

Does not invent unreadable details. Original is preserved; do not apply when
sharpening damages fine print. Requires OpenCV and NumPy (the project uses both).
"""
from __future__ import annotations
import argparse
from pathlib import Path
import cv2


def enhance(src: str | Path, dst: str | Path, scale: float = 3.0) -> None:
    image=cv2.imread(str(src))
    if image is None:
        raise ValueError(f'Cannot read image: {src}')
    if not (1.0 <= scale <= 4.0):
        raise ValueError('Scale should be between 1 and 4')
    enlarged=cv2.resize(image, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
    # Gentle, local contrast + a limited unsharp mask. Avoid aggressive
    # binarization because it can destroy thin receipt characters.
    lab=cv2.cvtColor(enlarged,cv2.COLOR_BGR2LAB)
    L,a,b=cv2.split(lab)
    clahe=cv2.createCLAHE(clipLimit=1.7,tileGridSize=(8,8))
    L=clahe.apply(L)
    enhanced=cv2.cvtColor(cv2.merge([L,a,b]),cv2.COLOR_LAB2BGR)
    smooth=cv2.GaussianBlur(enhanced,(0,0),sigmaX=1.1)
    enhanced=cv2.addWeighted(enhanced,1.3,smooth,-.3,0)
    Path(dst).parent.mkdir(parents=True,exist_ok=True)
    if not cv2.imwrite(str(dst),enhanced):
        raise IOError(f'Cannot write output image: {dst}')


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('input',type=Path)
    parser.add_argument('output',type=Path)
    parser.add_argument('--scale',type=float,default=3.0)
    args=parser.parse_args()
    enhance(args.input,args.output,args.scale)
    print(f'Enhanced image saved: {args.output}')
