"""Vendor model — allows deduplication / normalization of vendor names."""
from __future__ import annotations
from typing import Optional
from pydantic import BaseModel


class Vendor(BaseModel):
    id: Optional[int] = None
    name: str
    normalized_name: Optional[str] = None
    address: Optional[str] = None
    tax_id: Optional[str] = None
    invoice_count: int = 0

    class Config:
        from_attributes = True

    @staticmethod
    def normalize(name: str) -> str:
        """Lowercase, strip punctuation/common suffixes for matching duplicates."""
        import re
        n = name.lower().strip()
        n = re.sub(r"[^\w\s]", "", n)
        for suffix in [" inc", " llc", " ltd", " corp", " co"]:
            if n.endswith(suffix):
                n = n[: -len(suffix)]
        return n.strip()
