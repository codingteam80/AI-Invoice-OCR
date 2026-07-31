"""Reusable Streamlit UI components (status badges, confidence bars, etc.)."""
import streamlit as st


def confidence_badge(score: float | None):
    score = score or 0.0
    if score >= 0.85:
        st.success(f"Confidence: {score*100:.0f}%")
    elif score >= 0.6:
        st.warning(f"Confidence: {score*100:.0f}%")
    else:
        st.error(f"Confidence: {score*100:.0f}%")


def status_badge(status: str):
    icons = {
        "processed": "✅", "needs_review": "⚠️",
        "failed": "❌", "pending": "⏳", "processing": "🔄",
    }
    st.write(f"{icons.get(status, '•')} **{status}**")
