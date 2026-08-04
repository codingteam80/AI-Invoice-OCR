"""Small helpers for image handling / thumbnails."""
from pathlib import Path
from PIL import Image


def make_thumbnail(image_path: str, out_path: str, size=(300, 300)) -> str:
    img = Image.open(image_path)
    img.thumbnail(size)
    img.save(out_path)
    return out_path


def get_image_dimensions(image_path: str) -> tuple[int, int]:
    with Image.open(image_path) as img:
        return img.size


def is_valid_image(file_path: str) -> bool:
    try:
        with Image.open(file_path) as img:
            img.verify()
        return True
    except Exception:
        return False
