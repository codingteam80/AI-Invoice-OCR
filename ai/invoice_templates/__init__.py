"""Vendor-specific invoice extraction templates.

Templates contain extraction instructions and deterministic post-processing
rules for invoice layouts that are sufficiently stable to justify them.
"""

from .watsons import WATSONS_TEMPLATE_NAME, WATSONS_PROMPT, WATSONS_VISION_PROMPT, normalize_watsons_extraction

TEMPLATES = {
    WATSONS_TEMPLATE_NAME: {
        "prompt": WATSONS_PROMPT,
        "vision_prompt": WATSONS_VISION_PROMPT,
        "post_process": normalize_watsons_extraction,
    },
}


def get_template(name: str | None):
    return TEMPLATES.get(name or "")
