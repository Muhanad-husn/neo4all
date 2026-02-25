"""
ui/components/phase_indicator.py — Reusable phase indicator widget.

Pure display component that renders the current application phase as a
horizontal row of labelled steps. No API calls, no business logic, no
StateManager dependency.

Usage:
    from ui.components.phase_indicator import render_phase_indicator

    render_phase_indicator(current_phase=2)
"""

import streamlit as st

# Phase labels corresponding to Phase enum values 0–4.
_PHASE_LABELS: list[str] = ["Init", "Schema", "Ingestion", "Extraction", "Curation"]


def render_phase_indicator(current_phase: int) -> None:
    """Render a horizontal phase indicator showing progress through phases 0–4.

    Args:
        current_phase: Integer value of the current Phase (0–4).
    """
    cols = st.columns(len(_PHASE_LABELS))
    for idx, (col, label) in enumerate(zip(cols, _PHASE_LABELS)):
        with col:
            if idx < current_phase:
                st.markdown(f":green[**✓ {label}**]")
            elif idx == current_phase:
                st.markdown(f":blue[**→ {label}**]")
            else:
                st.markdown(f":gray[○ {label}]")
