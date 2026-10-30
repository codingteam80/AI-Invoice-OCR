"""Review warnings without exposing internal invoice or cell confidence scores.

Client-approved interface: actionable validation issues may be shown, whereas
OCR/evidence percentages remain internal. Field-specific warnings are shown as
amber highlights in the History detail panel.
"""
import streamlit as st
from config.field_visibility import visible_validation_issues


def render_quality_details(invoice):
    issues = visible_validation_issues(invoice.get('validation_issues') or [])
    for issue in issues:
        st.warning(str(issue))
    if invoice.get('status') == 'needs_review' and not issues:
        st.warning('Review required: verify the extracted fields against the original document.')
