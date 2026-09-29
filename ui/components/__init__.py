"""Reusable Streamlit UI components (status badges, confidence bars, etc.)."""
import streamlit as st


def status_badge(status: str):
    icons = {
        "processed": "✅", "needs_review": "⚠️",
        "failed": "❌", "pending": "⏳", "processing": "🔄",
    }
    st.write(f"{icons.get(status, '•')} **{status}**")
