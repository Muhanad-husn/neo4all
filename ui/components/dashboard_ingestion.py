"""
ui/components/dashboard_ingestion.py — Ingestion tab for the dashboard.

Shows documents with parser tier, expandable chunks, and quality charts.

Architecture rules:
  - No business logic (CLAUDE.md §4.1, SKILL-B R-B3).
  - No st.session_state access outside StateManager (CLAUDE.md §8).
"""

from __future__ import annotations

from typing import Any

import streamlit as st

from ui.components.api_fetch import fetch
from ui.components.plotly_helpers import bar_chart, donut_chart


def render_ingestion_tab(run_id: str) -> None:
    """Render the Ingestion tab content."""
    st.subheader("Ingestion Pipeline")

    doc_data = fetch(f"/api/documents/{run_id}")

    if doc_data is None:
        st.warning("Cannot reach API \u2014 ingestion data unavailable.")
        return

    documents: list[dict[str, Any]] = doc_data.get("documents", [])
    total_count: int = doc_data.get("total_count", len(documents))

    if not documents:
        st.info("No documents ingested yet for this run.")
        return

    total_chunks = sum(d.get("chunk_count", 0) for d in documents)

    # Summary metrics
    c1, c2 = st.columns(2)
    c1.metric("Documents Ingested", total_count)
    c2.metric("Total Chunks", total_chunks)

    # Parser tier distribution
    parser_counts: dict[str, int] = {}
    for d in documents:
        parser = d.get("parser_used", "") or "unknown"
        parser_counts[parser] = parser_counts.get(parser, 0) + 1

    if parser_counts:
        fig = bar_chart(
            list(parser_counts.keys()),
            list(parser_counts.values()),
            "Parser Tier Distribution",
        )
        st.plotly_chart(fig, width="stretch")

    st.divider()

    # Document table
    st.markdown("**Documents**")
    rows = [
        {
            "Doc ID": (d.get("doc_id") or "")[:16] + "\u2026",
            "Source": d.get("source_identity", "\u2014") or "\u2014",
            "Chunks": d.get("chunk_count", 0),
            "Parser": d.get("parser_used", "\u2014") or "\u2014",
            "Ingested At": d.get("created_at", "\u2014"),
        }
        for d in documents
    ]
    st.dataframe(rows, width="stretch", hide_index=True)

    # Per-document chunk expander
    for d in documents:
        doc_id = d.get("doc_id", "")
        doc_id_short = doc_id[:16] + "\u2026" if doc_id else "?"
        source = d.get("source_identity", "") or doc_id_short

        with st.expander(f"Chunks for {source}"):
            chunk_data = fetch(f"/api/documents/{run_id}/{doc_id}/chunks")
            if chunk_data is None or chunk_data.get("status") == "error":
                st.caption("Could not load chunks.")
                continue

            chunks: list[dict[str, Any]] = chunk_data.get("chunks", [])
            if not chunks:
                st.caption("No chunks found.")
                continue

            chunk_rows = [
                {
                    "Chunk ID": (c.get("chunk_id") or "")[:16] + "\u2026",
                    "Characters": c.get("char_count", 0),
                    "Pages": f"{c.get('start_page', '?')}\u2013{c.get('end_page', '?')}",
                    "Quality": (
                        ", ".join(c.get("quality_flags", [])) if c.get("quality_flags") else "clean"
                    ),
                }
                for c in chunks
            ]
            st.dataframe(chunk_rows, width="stretch", hide_index=True)

    # Quality flag distribution across all documents
    all_clean = 0
    all_flagged = 0
    for d in documents:
        doc_id = d.get("doc_id", "")
        chunk_data = fetch(f"/api/documents/{run_id}/{doc_id}/chunks")
        if chunk_data and chunk_data.get("status") == "success":
            for c in chunk_data.get("chunks", []):
                if c.get("quality_flags"):
                    all_flagged += 1
                else:
                    all_clean += 1

    if all_clean + all_flagged > 0:
        st.divider()
        fig = donut_chart(
            ["Clean", "Flagged"],
            [all_clean, all_flagged],
            "Chunk Quality Distribution",
        )
        st.plotly_chart(fig, width="stretch")
