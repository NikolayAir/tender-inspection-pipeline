"""Streamlit interface for the Tender-to-Inspection Brief application.

Run from the repository root:
    streamlit run src/app/streamlit_app.py

The interface displays persisted review briefs for bundled samples and supports
session-only analysis of pasted public excerpts. Synthetic documents are
identified as offline test data; curated public documents show their recorded
source URL.
"""

from __future__ import annotations

import logging
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

# Allow `streamlit run src/app/streamlit_app.py` to import the `src` package by
# ensuring the repository root is on the path.
ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import streamlit as st  # noqa: E402

from src.ai.risk_extract import (  # noqa: E402
    KEYWORD_DOMAINS,
    MISSING_INFO_PHRASES,
    extract_brief,
)
from src.collect.sample_loader import build_document_from_text  # noqa: E402
from src.db import database  # noqa: E402
from src.exports import VersionedBriefExport, serialize_brief_export  # noqa: E402
from src.models import (  # noqa: E402
    EvidenceSnippet,
    ReviewerDecision,
    ReviewState,
    ReviewTargetType,
    StoredReviewerDecision,
)
from src.pipeline import ensure_bundled_samples  # noqa: E402
from src.validation.manual_validation import (  # noqa: E402
    humanize_validation_label,
    load_validation_summary,
)

logger = logging.getLogger(__name__)

# Upper bound on pasted text, to keep the in-session preview responsive.
MAX_ADHOC_CHARS = 50_000
REVIEW_STATE_OPTIONS: tuple[ReviewState, ...] = (
    "accepted",
    "rejected",
    "needs_follow_up",
)
REVIEW_STATE_LABELS: dict[ReviewState | None, str] = {
    None: "Select a decision",
    "accepted": "Accepted",
    "rejected": "Rejected",
    "needs_follow_up": "Needs follow-up",
}


def category_for_term(term: str) -> str:
    """Map a matched term to a readable category for the evidence table.

    Display-only label lookup (not extraction logic): missing-information phrases
    become "Missing information", risk keywords map to their domain. The term is
    normalised with lower().strip() so matching is case-insensitive.
    """
    normalized = term.lower().strip()
    if normalized in {p.lower().strip() for p in MISSING_INFO_PHRASES}:
        return "Missing information"
    for domain, keywords in KEYWORD_DOMAINS.items():
        if normalized in {k.lower().strip() for k in keywords}:
            return domain
    return "Other"


def adhoc_input_error(text: str, max_chars: int = MAX_ADHOC_CHARS) -> str | None:
    """Validate pasted text; return a user-facing message, or None if OK.

    Pure (non-UI) so it can be unit-tested: empty/whitespace input and over-length
    input are rejected; valid input returns None.
    """
    if not text or not text.strip():
        return "Please paste a public tender or document excerpt to preview."
    if len(text) > max_chars:
        return (
            f"Excerpt is too long ({len(text):,} characters). "
            f"Please paste at most {max_chars:,} characters."
        )
    return None


def brief_export_filename(document_id: int) -> str:
    """Return the deterministic filename used for one persisted brief export."""
    return f"inspection-brief-document-{document_id}.json"


def evidence_rows(evidence: list[EvidenceSnippet]) -> list[dict]:
    """Build list-of-dict rows for st.dataframe (no pandas dependency)."""
    return [
        {
            "Category": category_for_term(ev.matched_term),
            "Matched term": ev.matched_term,
            "Location": ev.location,
            "Snippet": ev.snippet,
        }
        for ev in evidence
    ]


def reviewer_widget_key(
    brief_id: int,
    target_type: ReviewTargetType,
    target_index: int,
    field: str,
) -> str:
    """Return a stable Streamlit key for one persisted review target."""
    return f"reviewer:{brief_id}:{target_type}:{target_index}:{field}"


def review_state_label(state: ReviewState | None) -> str:
    """Return the user-facing label for a stored or unselected review state."""
    return REVIEW_STATE_LABELS[state]


def reviewer_history_rows(
    history: list[StoredReviewerDecision],
    target_type: ReviewTargetType,
    target_index: int,
) -> list[dict]:
    """Build newest-first display rows for one target's decision history."""
    target_history = [
        event
        for event in history
        if event.target_type == target_type and event.target_index == target_index
    ]
    return [
        {
            "Decided at": event.decided_at.astimezone(timezone.utc).strftime(
                "%Y-%m-%d %H:%M:%S UTC"
            ),
            "State": review_state_label(event.state),
            "Note": event.note or "—",
        }
        for event in reversed(target_history)
    ]


def _bullets(items: list[str], empty_text: str) -> None:
    """Render a list of strings as markdown bullets, or an italic empty note."""
    if not items:
        st.markdown(f"*{empty_text}*")
        return
    st.markdown("\n".join(f"- {item}" for item in items))


def render_brief_body(brief) -> None:
    """Render the shared review-brief body for bundled and pasted sources.

    Source provenance and the validation snapshot remain in the calling view
    because they depend on how the brief was created.
    """
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Review focus areas", len(brief.risk_domains))
    m2.metric("Detected information gaps", len(brief.missing_info))
    m3.metric("Evidence snippets", len(brief.evidence))
    m4.metric(
        "Baseline confidence",
        brief.confidence.capitalize(),
        help=(
            "Fixed at Low because the rule-based baseline requires human review; "
            "this is not a statistical confidence score."
        ),
    )

    st.write(f"**Generated summary:** {brief.summary}")
    st.info(
        "Human review required. Verify each generated finding against its "
        "source evidence before using it."
    )

    st.markdown("**Detected technical scopes**")
    _bullets(brief.technical_scopes, "No technical scopes detected by the baseline")

    st.markdown("**Potential review focus areas**")
    _bullets(brief.risk_domains, "No review focus areas detected by the baseline")
    st.caption(
        "Current limitation: technical scopes and review focus areas use the "
        "same set of domain rules, so the two sections may overlap."
    )

    st.markdown("**Missing / unclear information**")
    _bullets(
        brief.missing_info,
        "No explicit information gaps detected by the baseline",
    )

    st.markdown("**Suggested review questions**")
    _bullets(brief.review_questions, "No review questions generated")

    st.markdown("**Evidence snippets (source-traced)**")
    rows = evidence_rows(brief.evidence)
    if rows:
        st.dataframe(
            rows,
            hide_index=True,
            width="stretch",
            column_config={
                "Category": st.column_config.TextColumn("Category", width="small"),
                "Matched term": st.column_config.TextColumn(
                    "Matched term", width="small"
                ),
                "Location": st.column_config.TextColumn("Location", width="small"),
                "Snippet": st.column_config.TextColumn("Snippet", width="large"),
            },
        )
    else:
        st.markdown("*No source evidence captured*")


def _render_reviewer_target(
    brief_id: int,
    target_type: ReviewTargetType,
    target_index: int,
    generated_value: str,
    current: StoredReviewerDecision | None,
    history: list[StoredReviewerDecision],
) -> None:
    """Render one generated finding with append-only reviewer controls."""
    flash_key = reviewer_widget_key(brief_id, target_type, target_index, "saved")
    message = st.session_state.pop(flash_key, None)
    if message:
        st.toast(message, icon="✅")

    st.markdown("**Generated finding**")
    st.write(generated_value)

    if current is None:
        st.write("**Current review status:**", "Unreviewed")
        st.caption("No reviewer decision has been saved for this finding.")
    else:
        st.write("**Current review status:**", review_state_label(current.state))
        st.write("**Current reviewer note:**", current.note or "No note provided")

    form_key = reviewer_widget_key(
        brief_id, target_type, target_index, "decision_form"
    )
    selected_index = (
        0
        if current is None
        else REVIEW_STATE_OPTIONS.index(current.state) + 1
    )

    with st.form(form_key):
        selected_state = st.selectbox(
            "Reviewer decision",
            options=(None, *REVIEW_STATE_OPTIONS),
            index=selected_index,
            format_func=review_state_label,
            key=reviewer_widget_key(
                brief_id, target_type, target_index, "state"
            ),
            help=(
                "Accept or reject the generated finding, or mark it as needing "
                "follow-up."
            ),
        )
        note = st.text_area(
            "Reviewer note (optional)",
            value=current.note if current and current.note else "",
            max_chars=2000,
            key=reviewer_widget_key(
                brief_id, target_type, target_index, "note"
            ),
            help="Saved notes are human-authored and limited to 2,000 characters.",
        )
        submitted = st.form_submit_button("Save decision")

    if submitted:
        if selected_state is None:
            st.warning("Select a reviewer decision before saving.")
        else:
            try:
                decision = ReviewerDecision(
                    brief_id=brief_id,
                    target_type=target_type,
                    target_index=target_index,
                    state=selected_state,
                    note=note,
                    decided_at=datetime.now(timezone.utc),
                )
                database.insert_reviewer_decision(decision)
            except (ValueError, sqlite3.Error):
                st.error(
                    "The reviewer decision could not be saved. "
                    "Refresh the page and try again."
                )
            else:
                st.session_state[flash_key] = "Reviewer decision saved."
                st.rerun()

    history_rows = reviewer_history_rows(history, target_type, target_index)
    if history_rows:
        with st.expander(f"Decision history ({len(history_rows)})", expanded=False):
            st.dataframe(
                history_rows,
                hide_index=True,
                width="stretch",
                column_config={
                    "Decided at": st.column_config.TextColumn(
                        "Decided at", width="medium"
                    ),
                    "State": st.column_config.TextColumn("State", width="small"),
                    "Note": st.column_config.TextColumn("Note", width="large"),
                },
            )


def render_reviewer_decisions(
    payload: VersionedBriefExport,
) -> None:
    """Render findings and decisions from the same snapshot as the JSON export."""
    brief_id = payload.brief_id
    brief = payload.brief
    st.subheader("Reviewer decisions")
    st.caption(
        "Generated findings remain unchanged. Each saved reviewer decision "
        "is recorded as a new history entry, preserving earlier decisions "
        "and notes."
    )

    history = [
        StoredReviewerDecision(brief_id=brief_id, **decision.model_dump())
        for decision in payload.reviewer_decisions
    ]
    latest = {(decision.target_type, decision.target_index): decision for decision in history}
    target_groups: tuple[
        tuple[ReviewTargetType, str, str, list[str]], ...
    ] = (
        (
            "risk_domain",
            "Generated review focus areas",
            "No review focus areas were generated for this brief.",
            brief.risk_domains,
        ),
        (
            "missing_info",
            "Generated information gaps",
            "No information gaps were generated for this brief.",
            brief.missing_info,
        ),
    )

    if not any(items for _, _, _, items in target_groups):
        st.info("This brief contains no generated findings that require decisions.")
        return

    for target_type, heading, empty_text, items in target_groups:
        st.markdown(f"### {heading}")
        if not items:
            st.caption(empty_text)
            continue

        for target_index, generated_value in enumerate(items):
            with st.container(border=True):
                _render_reviewer_target(
                    brief_id=brief_id,
                    target_type=target_type,
                    target_index=target_index,
                    generated_value=generated_value,
                    current=latest.get((target_type, target_index)),
                    history=history,
                )


def _ensure_demo_data() -> list[dict]:
    """Return stored documents after recovering incomplete bundled samples.

    Supports hosted demos (e.g. Streamlit Community Cloud) where the app is opened
    directly from this file without first running ``python -m src.pipeline``. Runs
    fully offline against the committed bundled samples. Completed results,
    including restored legacy provenance and their reviewer decisions, are
    retained. The explicit pipeline command remains the local path for refreshing
    results and checking reproducibility.
    """
    ensure_bundled_samples()
    return database.get_documents()


def render_bundled_view() -> None:
    """Render persisted review briefs for bundled samples."""
    try:
        documents = _ensure_demo_data()
    except Exception:
        logger.exception("Automatic bundled-sample recovery failed")
        try:
            documents = database.get_documents()
        except (OSError, sqlite3.Error):
            logger.exception("Could not read saved documents after recovery failed")
            st.error("Saved documents are currently unavailable. Refresh to retry.")
            return

        if documents:
            st.warning(
                "Some bundled samples could not be prepared. Showing saved "
                "documents. Refresh to retry."
            )

    if not documents:
        st.error(
            "No documents found and automatic sample initialisation failed. "
            "Run `python -m src.pipeline` from the repository root, then refresh."
        )
        return

    labels = {f"#{d['id']} - {d['title']}": d for d in documents}
    choice = st.selectbox("Select a document", list(labels.keys()), key="bundled_document")
    document = labels[choice]

    try:
        snapshot = database.get_document_review(document["id"])
    except (OSError, sqlite3.Error):
        logger.exception("Could not read the saved review for document %s", document["id"])
        st.error("The saved review for this document is currently unavailable. Refresh to retry.")
        return

    if snapshot is None:
        st.info("The selected document is no longer available. Refresh to update the list.")
        return
    document, export_payload = snapshot

    is_synthetic = document["source"] == "synthetic_sample"
    if is_synthetic:
        st.info(
            "This document is synthetic sample data for offline testing. "
            "It is not a real public tender."
        )
    else:
        source_url = document.get("source_url") or ""
        url_part = f" View the full notice: {source_url}" if source_url else ""
        st.info(
            f"This is a manually curated excerpt from a public procurement notice "
            f"(source: {document['source']}).{url_part}"
        )

    st.subheader("Inspection brief")
    if export_payload is None:
        st.info("No brief stored for this document.")
        return

    st.download_button(
        "Download review brief (JSON)",
        data=serialize_brief_export(export_payload),
        file_name=brief_export_filename(document["id"]),
        mime="application/json",
        help=(
            "Includes the review brief, source evidence, document metadata, "
            "processing time and versions, and the source fingerprint."
        ),
    )

    render_brief_body(export_payload.brief)
    render_reviewer_decisions(export_payload)

    expander_label = (
        "Source document — synthetic sample"
        if is_synthetic
        else "Source excerpt — public sample"
    )
    with st.expander(expander_label, expanded=False):
        st.write(f"**Source:** {document['source']}")
        url_display = document["source_url"] or (
            "n/a — offline synthetic sample" if is_synthetic else "n/a"
        )
        st.write(f"**Source URL:** {url_display}")
        st.text(document["clean_text"])

    with st.expander("Validation snapshot", expanded=False):
        try:
            summary = load_validation_summary()
        except Exception:
            st.caption("Validation summary is currently unavailable.")
        else:
            status_bits = ", ".join(
                f"{humanize_validation_label(status)} ({count})"
                for status, count in sorted(summary["status_counts"].items())
                if status
            )
            st.write(f"**Manually reviewed samples:** {summary['sample_count']}")
            st.write(
                f"**Manual comparison outcomes:** {status_bits}"
                if status_bits
                else "**Manual comparison outcomes:** n/a"
            )
            detail_rows = summary.get("detail_rows", [])
            if detail_rows:
                st.dataframe(
                    detail_rows,
                    hide_index=True,
                    width="stretch",
                    column_config={
                        "sample_id": st.column_config.TextColumn("Sample", width="small"),
                        "source_type": st.column_config.TextColumn(
                            "Source type", width="small"
                        ),
                        "match_status": st.column_config.TextColumn("Match", width="small"),
                        "manually_expected_domains": st.column_config.TextColumn(
                            "Manually expected domains", width="large"
                        ),
                        "extracted_domains": st.column_config.TextColumn(
                            "Extracted domains", width="large"
                        ),
                    },
                )
            st.caption(
                'Here, "Match" means that the extracted review domains agreed with '
                "the manually defined expectations for a sample. This dataset is "
                "too small to support statistical accuracy claims. Detailed notes "
                "are stored in `data/labels/manual_validation_v1.csv`."
            )


def render_adhoc_view() -> None:
    """Render session-only analysis for a pasted public excerpt."""
    st.info(
        "Paste a public, non-confidential excerpt to generate a review brief. "
        "It is processed only in this session and is not saved."
    )

    title = st.text_input(
        "Document title (optional)",
        help="Used only if the pasted text does not contain a TITLE: line.",
    )
    source_url = st.text_input(
        "Source URL (optional)",
        help="Used for reference only; the application does not open or fetch the URL.",
    )
    text = st.text_area(
        "Paste a public tender or technical document excerpt",
        height=240,
        help="Public, non-confidential text only; maximum 50,000 characters.",
    )

    if not st.button("Generate review brief"):
        return

    error = adhoc_input_error(text)
    if error:
        st.warning(error)
        return

    document = build_document_from_text(
        text,
        source_url=source_url.strip(),
        default_title=title.strip() or "Pasted public excerpt",
    )
    brief = extract_brief(document)

    st.subheader("Review brief preview")
    render_brief_body(brief)
    st.caption(
        "This preview is not stored. Validation results for bundled samples "
        "do not apply to pasted excerpts."
    )


def render() -> None:
    """Render bundled review briefs and the public-excerpt analysis view."""
    st.set_page_config(
        page_title="Tender-to-Inspection Brief", page_icon=":clipboard:", layout="wide"
    )

    st.title("Tender-to-Inspection Brief")
    st.caption(
        "Creates source-traced technical review briefs from public construction "
        "tender notices and excerpts."
    )
    st.markdown(
        "**Current coverage:** 3 bundled samples, including "
        "2 public Luxembourg procurement excerpts · SQLite-backed briefs and "
        "reviewer decisions · transparent rule-based extraction · qualitative "
        "validation"
    )
    st.warning(
        "Verify generated findings against source evidence. "
        "The application supports technical review, not legal, regulatory, "
        "compliance, safety, or engineering decisions."
    )

    tab_bundled, tab_adhoc = st.tabs(["Bundled samples", "Analyse a public excerpt"])
    with tab_bundled:
        render_bundled_view()
    with tab_adhoc:
        render_adhoc_view()


if __name__ == "__main__":
    render()
