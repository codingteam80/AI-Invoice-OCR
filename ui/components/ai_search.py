"""Reusable Streamlit UI for local Qwen natural-language search."""
from __future__ import annotations

import streamlit as st

from ai.search_interpreter import SearchInterpretationError, interpret_natural_language_search
from config.settings import settings
from services.search_service import summarize_search_plan


def _clear_ai_search(query_key: str, plan_key: str, applied_query_key: str, warnings_key: str) -> None:
    st.session_state[query_key] = ""
    st.session_state.pop(plan_key, None)
    st.session_state.pop(applied_query_key, None)
    st.session_state.pop(warnings_key, None)


def render_ai_search(
    *,
    page: str,
    key: str,
    placeholder: str,
    context: dict | None = None,
    scope_note: str | None = None,
) -> dict | None:
    """Render one AI search box and return the currently applied search plan."""
    query_key = f"ai_search_query_{key}"
    plan_key = f"ai_search_plan_{key}"
    applied_query_key = f"ai_search_applied_query_{key}"
    warnings_key = f"ai_search_warnings_{key}"

    st.markdown(f"**🤖 AI Search ({settings.SEARCH_LLM_MODEL})**")
    query = st.text_input(
        "Natural-language search",
        key=query_key,
        placeholder=placeholder,
        label_visibility="collapsed",
    )
    b1, b2, spacer = st.columns([1.35, 1.35, 6])
    run_search = b1.button("🔍 Search", key=f"ai_search_run_{key}", type="primary")
    b2.button(
        "Clear AI Search",
        key=f"ai_search_clear_{key}",
        on_click=_clear_ai_search,
        args=(query_key, plan_key, applied_query_key, warnings_key),
    )

    if run_search:
        if not query.strip():
            st.warning("Enter a natural-language search request first.")
        else:
            try:
                with st.spinner(f"{settings.SEARCH_LLM_MODEL} is interpreting your search..."):
                    plan, warnings = interpret_natural_language_search(
                        query,
                        page=page,
                        context=context,
                    )
                st.session_state[plan_key] = plan
                st.session_state[applied_query_key] = query.strip()
                st.session_state[warnings_key] = warnings
            except SearchInterpretationError as exc:
                st.error(str(exc))

    plan = st.session_state.get(plan_key)
    applied_query = st.session_state.get(applied_query_key)
    warnings = st.session_state.get(warnings_key) or []
    if plan is not None:
        st.info(
            f"**AI interpreted:** {summarize_search_plan(plan)}"
            + (f"\n\n**Applied request:** {applied_query}" if applied_query else "")
        )
        for warning in warnings:
            st.warning(warning)
    if scope_note:
        st.caption(scope_note)
    return plan
