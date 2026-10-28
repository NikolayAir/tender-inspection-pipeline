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
from html import escape
from json import JSONDecodeError
from pathlib import Path

# Allow `streamlit run src/app/streamlit_app.py` to import the `src` package by
# ensuring the repository root is on the path.
ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import streamlit as st  # noqa: E402
from pydantic import ValidationError  # noqa: E402

from src.ai.risk_extract import NO_MATCH_NOTICE, extract_brief  # noqa: E402
from src.app.export_preview import render_export_actions  # noqa: E402
from src.app.guide import show_workflow_guide  # noqa: E402
from src.app.review import (  # noqa: E402
    REVIEW_FILTERS,
    REVIEW_STATE_OPTIONS,
    ReviewSubmission,
    brief_findings,
    category_for_term,
    escape_markdown,
    evidence_for_finding,
    filter_findings,
    information_gaps,
    latest_decisions,
    review_state_label,
)
from src.app.scroll import render_pending_scroll, request_scroll  # noqa: E402
from src.collect.sample_loader import build_document_from_text  # noqa: E402
from src.db import database  # noqa: E402
from src.exports import VersionedBriefExport, serialize_brief_export  # noqa: E402
from src.models import (  # noqa: E402
    InspectionBrief,
    ReviewerDecision,
    ReviewTargetType,
    StoredReviewerDecision,
)
from src.pipeline import ensure_bundled_samples  # noqa: E402
from src.validation.manual_validation import (  # noqa: E402
    humanize_validation_label,
    load_validation_summary,
)

logger = logging.getLogger(__name__)

APP_TITLE = "Tender-to-Inspection Brief"

# Upper bound on pasted text, to keep the in-session preview responsive.
MAX_ADHOC_CHARS = 50_000


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


def reviewer_widget_key(
    brief_id: int,
    target_type: ReviewTargetType,
    target_index: int,
    field: str,
) -> str:
    """Return a stable widget or session key for one persisted review target."""
    return f"reviewer:{brief_id}:{target_type}:{target_index}:{field}"


def _remember_reviewer_draft(
    brief_id: int, target_type: ReviewTargetType, target_index: int,
) -> None:
    """Retain edited fields in the session without writing a decision."""
    st.session_state[reviewer_widget_key(brief_id, target_type, target_index, "draft")] = {
        "state": st.session_state[reviewer_widget_key(brief_id, target_type, target_index, "state")],
        "note": st.session_state[reviewer_widget_key(brief_id, target_type, target_index, "note")],
    }
    st.session_state.pop(reviewer_widget_key(brief_id, target_type, target_index, "error"), None)


def _persist_review_submission(brief_id: int, submission: ReviewSubmission | None) -> bool:
    """Save only after a full rerun has read the current brief snapshot."""
    if submission is None:
        return False
    if submission["brief_id"] != brief_id:
        st.warning("The selected brief has changed. The previous decision was not saved.")
        return False
    identity = brief_id, submission["target_type"], submission["target_index"]
    error_key = reviewer_widget_key(*identity, "error")
    try:
        decision = ReviewerDecision(
            **submission, decided_at=datetime.now(timezone.utc),
        )
        database.insert_reviewer_decision(decision)
    except ValueError:
        logger.exception("Invalid reviewer decision for brief %s", brief_id)
        st.session_state[error_key] = (
            "The decision could not be saved for this finding. "
            "Copy your note before refreshing, then reopen the finding.", False,
        )
    except sqlite3.Error:
        logger.exception("Could not save reviewer decision for brief %s", brief_id)
        st.session_state[error_key] = (
            "Your decision was not saved. Your changes remain in this form; "
            "try saving again.", True,
        )
    else:
        st.session_state.pop(reviewer_widget_key(*identity, "draft"), None)
        st.session_state.pop(error_key, None)
        st.session_state[reviewer_widget_key(*identity, "saved")] = "Reviewer decision saved."
        return True
    return False


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
    """Keep source-derived values literal, or show an italic empty note."""
    if not items:
        st.markdown(f"*{empty_text}*")
        return
    with st.container(gap="small"):
        for item in items:
            st.text(f"• {item}")


def _select_brief_tab(label: str) -> None:
    st.session_state["brief_tabs"] = label
    request_scroll("review")


def _render_information_gaps(brief: InspectionBrief) -> None:
    """Distinguish extracted gaps from the baseline's generic review notice."""
    st.markdown("**Information gaps**")
    _bullets(
        information_gaps(brief),
        "No explicit phrases about missing information matched. Check the full tender documentation.",
    )
    if NO_MATCH_NOTICE in brief.missing_info:
        st.info(NO_MATCH_NOTICE)


def _render_scope_tags(scopes: list[str]) -> None:
    """Treat generated scope names as text inside decorative tags."""
    tags = "".join(f'<span class="scope-tag">{escape(scope)}</span>' for scope in scopes)
    st.html(f'<div class="scope-tags">{tags}</div>')


def render_brief_body(brief: InspectionBrief, *, persisted: bool = False) -> None:
    """Show a compact overview without duplicating scopes and focus areas."""
    with st.container(key="overview_layout"):
        summary, questions = st.columns([1.1, 1], gap="large")
    with summary, st.container(border=True, key="overview_summary"):
        st.subheader("At a glance", anchor=False)
        total = len(brief.risk_domains) + len(brief.missing_info)
        st.write(
            f"{total} generated {'finding' if total == 1 else 'findings'} to verify "
            "against the tender documentation."
        )
        if persisted and total:
            st.button(
                "Start reviewing", icon=":material/arrow_forward:", type="primary",
                on_click=_select_brief_tab, args=("Review findings",),
            )
        st.markdown("**Detected technical scopes**")
        if brief.technical_scopes:
            _render_scope_tags(brief.technical_scopes)
        else:
            st.caption("No technical topics matched the keyword rules. Read the source manually.")
        if brief.risk_domains != brief.technical_scopes:
            st.markdown("**Review focus areas**")
            _bullets(brief.risk_domains, "No review focus areas detected.")
        _render_information_gaps(brief)
    with questions:
        st.subheader("Questions to check", anchor=False)
        st.caption("Suggested follow-up questions for checking the full tender documentation.")
        if brief.review_questions:
            for index, question in enumerate(brief.review_questions, 1):
                st.markdown(f"**{index:02d}** · {escape_markdown(question)}")
        else:
            st.write("No questions generated. Start by reading the source excerpt.")


def render_evidence_inspector(brief: InspectionBrief, *, key: str) -> None:
    """Read complete source snippets without a horizontally clipped table."""
    if not brief.evidence:
        st.info("No source evidence was captured. Read the source excerpt manually.")
        return
    indices = list(range(len(brief.evidence)))
    if st.session_state.get(key, 0) not in indices:
        st.session_state[key] = 0
    selected = st.selectbox(
        "Evidence snippet", indices, key=key,
        format_func=lambda index: (
            f"{category_for_term(brief.evidence[index].matched_term)} "
            f"· {brief.evidence[index].matched_term}"
        ),
    )
    snippet = brief.evidence[selected]
    with st.container(border=True, key="evidence_quote"):
        st.markdown(f"**{category_for_term(snippet.matched_term)}**")
        st.caption(f'Matched term: "{snippet.matched_term}" · {snippet.location} (approximate)')
        st.text(snippet.snippet)
    st.caption(f"Source match {selected + 1} of {len(brief.evidence)}. Keyword matches require human verification.")


def _render_reviewer_target(
    brief_id: int,
    target_type: ReviewTargetType,
    target_index: int,
    generated_value: str,
    current: StoredReviewerDecision | None,
    history: list[StoredReviewerDecision],
    *,
    show_finding: bool = True,
) -> None:
    """Render one generated finding with append-only reviewer controls."""
    flash_key = reviewer_widget_key(brief_id, target_type, target_index, "saved")
    message = st.session_state.pop(flash_key, None)
    if message:
        st.toast(message, icon="✅")

    if show_finding:
        st.markdown("**Generated finding**")
        st.text(generated_value)

    if current is None:
        st.write("**Current review status:**", "Unreviewed")
        if show_finding:
            st.caption("No reviewer decision has been saved for this finding.")
    else:
        st.write("**Current review status:**", review_state_label(current.state))
        if show_finding:
            st.markdown("**Current reviewer note:**")
            st.text(current.note or "No note provided")

    draft_key = reviewer_widget_key(brief_id, target_type, target_index, "draft")
    error_key = reviewer_widget_key(brief_id, target_type, target_index, "error")
    # These non-widget keys survive when another finding's controls are shown.
    draft = st.session_state.get(draft_key, {})
    initial_state = draft.get("state", current.state if current else None)
    selected_index = (
        0
        if initial_state is None
        else REVIEW_STATE_OPTIONS.index(initial_state) + 1
    )
    selected_state = st.selectbox(
        "Reviewer decision", options=(None, *REVIEW_STATE_OPTIONS),
        index=selected_index, format_func=review_state_label,
        key=reviewer_widget_key(brief_id, target_type, target_index, "state"),
        on_change=_remember_reviewer_draft, args=(brief_id, target_type, target_index),
        help="Accept or reject the generated finding, or mark it as needing follow-up.",
    )
    saved_note = current.note or "" if current else ""
    note = st.text_area(
        "Reviewer note (optional)",
        value=draft.get("note", saved_note),
        max_chars=2000, height=110,
        key=reviewer_widget_key(brief_id, target_type, target_index, "note"),
        on_change=_remember_reviewer_draft, args=(brief_id, target_type, target_index),
        help="Explain your decision or what needs follow-up. Use the save button to record the note. Up to 2,000 characters.",
    ) or ""
    # Compare notes using the storage model's trimming and blank-to-None rule.
    matches_saved = (
        current is not None and selected_state == current.state
        and (note.strip() or None) == current.note
    )
    dirty = not matches_saved and (
        current is not None or selected_state is not None or bool(note.strip())
    )
    save_error = st.session_state.get(error_key)
    if matches_saved and not save_error:
        st.session_state.pop(draft_key, None)
    # The second tuple item marks a retryable failure. A failed Save again still
    # needs Retry save even when its fields match the previous saved event.
    if save_error and save_error[1]:
        save_label, save_icon = "Retry save", ":material/refresh:"
    elif matches_saved and not save_error:
        save_label, save_icon = "Save again", ":material/check_circle:"
    elif current is not None:
        save_label, save_icon = "Save changes", ":material/save:"
    else:
        save_label, save_icon = "Save decision", ":material/save:"
    submitted = st.button(
        save_label, icon=save_icon, width="stretch",
        type="secondary" if matches_saved and not save_error else "primary",
        key=reviewer_widget_key(brief_id, target_type, target_index, "save"),
        help="Save another history entry with these values." if matches_saved and not save_error
        else "Save this decision and note to the selected brief.",
    )

    if submitted:
        st.session_state[draft_key] = {"state": selected_state, "note": note}
        if selected_state is None:
            st.warning("Select a reviewer decision before saving.")
        else:
            st.session_state["_pending_review_save"] = ReviewSubmission(
                brief_id=brief_id, target_type=target_type, target_index=target_index,
                state=selected_state, note=note,
            )
            st.rerun(scope="app")

    if save_error:
        st.error(save_error[0])
    elif matches_saved:
        st.badge("Decision saved", icon=":material/check:", color="green")
    elif dirty:
        st.badge("Unsaved changes", icon=":material/edit:", color="orange")

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


def _move_finding(
    key: str, identities: list[tuple[ReviewTargetType, int]], offset: int,
) -> None:
    """Move within the visible queue without changing persisted target indices."""
    current = st.session_state.get(key)
    # A queued click may arrive after the selection or filter has changed.
    if current not in identities:
        return
    next_index = identities.index(current) + offset
    if 0 <= next_index < len(identities):
        st.session_state[key] = identities[next_index]


# Editing fields or changing findings keeps the displayed snapshot. Saving asks
# for a full rerun so the current brief is checked before writing.
@st.fragment
def render_reviewer_decisions(payload: VersionedBriefExport) -> None:
    """Review one original finding beside its supporting source evidence."""
    st.caption("Check the source, then save your decision. Unsaved changes stay in this browser session.")
    findings = brief_findings(payload.brief)
    if not findings:
        st.info("This brief contains no generated findings that require decisions.")
        return

    # Saving under a status filter can remove the previous finding from view.
    # Consume its confirmation here so it is still shown once, even for an empty queue.
    for finding in findings:
        flash_key = reviewer_widget_key(payload.brief_id, *finding.identity, "saved")
        message = st.session_state.pop(flash_key, None)
        if message:
            st.toast(message, icon="✅")

    latest = latest_decisions(payload)
    history = [
        StoredReviewerDecision(brief_id=payload.brief_id, **event.model_dump())
        for event in payload.reviewer_decisions
    ]
    with st.container(key="review_controls"):
        status_column, finding_column, previous, following = st.columns(
            [1, 2.7, 0.65, 0.65], vertical_alignment="bottom",
        )
    with status_column:
        status = st.selectbox(
            "Review status", list(REVIEW_FILTERS), key=f"review_filter:{payload.brief_id}",
        )
    visible = filter_findings(findings, latest, status)
    if not visible:
        st.info("No findings match this status. Choose another review status to continue.")
        return

    identities = [finding.identity for finding in visible]
    finding_map = {finding.identity: finding for finding in visible}
    queue_key = f"review_queue:{payload.brief_id}"
    if st.session_state.get(queue_key) not in identities:
        st.session_state[queue_key] = identities[0]

    def label(identity):
        finding = finding_map[identity]
        current = latest.get(identity)
        state = review_state_label(current.state) if current else "Unreviewed"
        value = finding.value if len(finding.value) <= 90 else finding.value[:87] + "…"
        return f"{finding.kind} · {value} · {state}"

    with finding_column:
        selected = st.selectbox(
            "Finding to review", identities, format_func=label, key=queue_key,
        )
    index = identities.index(selected)
    previous.button(
        "Previous", icon=":material/arrow_back:", width="stretch", disabled=index == 0,
        on_click=_move_finding, args=(queue_key, identities, -1),
    )
    following.button(
        "Next", icon=":material/arrow_forward:", icon_position="right", width="stretch",
        disabled=index == len(identities) - 1,
        on_click=_move_finding, args=(queue_key, identities, 1),
    )
    st.caption(f"Finding {index + 1} of {len(visible)} in this view · {len(latest)} of {len(findings)} have saved decisions")

    finding = finding_map[selected]
    with st.container(key="review_layout"):
        source, decision = st.columns([1.15, 1], gap="large")
    with source, st.container(border=True, key="finding_evidence"):
        st.badge(finding.kind, color="gray")
        st.subheader(escape_markdown(finding.value), anchor=False)
        st.markdown("**Supporting source evidence**")
        snippets = evidence_for_finding(payload.brief, finding)
        for snippet in snippets:
            st.caption(f'Matched term: "{snippet.matched_term}" · {snippet.location} (approximate)')
            st.text(snippet.snippet)
        if not snippets:
            st.info("No direct supporting snippet was captured for this finding. Read the full source excerpt before deciding.")
    with decision, st.container(border=True, key="finding_decision"):
        st.subheader("Your decision", anchor=False)
        _render_reviewer_target(
            brief_id=payload.brief_id,
            target_type=finding.target_type,
            target_index=finding.target_index,
            generated_value=finding.value,
            current=latest.get(selected),
            history=history,
            show_finding=False,
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
    # Consume a save once, including when its document can no longer be read.
    submission = st.session_state.pop("_pending_review_save", None)
    try:
        documents = _ensure_demo_data()
    except Exception:
        logger.exception("Automatic bundled-sample recovery failed")
        # If a sample cannot be prepared, keep other saved reviews and downloads available.
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
    with st.sidebar:
        st.divider()
        st.caption("BUNDLED SAMPLES")
        choice = st.selectbox("Select a document", list(labels.keys()), key="bundled_document")
        st.caption("Saved results and reviewer decisions are retained between visits.")
    document = labels[choice]

    saved = False
    try:
        snapshot = database.get_document_review(document["id"])
        if snapshot is not None and snapshot[1] is not None:
            saved = _persist_review_submission(snapshot[1].brief_id, submission)
            if saved:
                # The write has committed; a later read error must still report that the decision was saved.
                snapshot = database.get_document_review(document["id"])
    except (JSONDecodeError, ValidationError, database.InvalidStoredTimestampError):
        logger.exception("Saved review data is invalid for document %s", document["id"])
        if saved:
            st.success("Reviewer decision saved.")
        st.error(
            "This saved review cannot be displayed because some stored data is invalid. "
            "Choose another document to continue."
        )
        return
    except (OSError, sqlite3.Error):
        logger.exception("Could not read the saved review for document %s", document["id"])
        if saved:
            st.success("Reviewer decision saved.")
        st.error("The saved review for this document is currently unavailable. Refresh to retry.")
        return

    if snapshot is None:
        st.info("The selected document is no longer available. Refresh to update the list.")
        return
    document, export_payload = snapshot

    if export_payload is None:
        st.info("No brief stored for this document.")
        return

    with st.container(key="brief_header"):
        heading, actions = st.columns([1, 1], vertical_alignment="center")
        with heading:
            st.subheader(escape_markdown(document["title"]), anchor=False)
            st.badge(
                "Synthetic test sample" if document["source"] == "synthetic_sample" else "Public tender excerpt",
                icon=":material/science:" if document["source"] == "synthetic_sample" else ":material/public:",
                color="gray",
            )
        with actions:
            render_export_actions(
                export_payload, serialize_brief_export(export_payload),
                brief_export_filename(document["id"]),
            )

    render_brief_metrics(export_payload.brief, export_payload)
    st.caption(
        "Human review required · Keyword-based findings · Confidence label: Low (fixed). "
        "Verify findings against the source before using them."
    )
    overview, decisions, evidence, details = st.tabs(
        ["Overview", "Review findings", "Evidence & source", "Run details"],
        key="brief_tabs",
        on_change="rerun",
    )
    with overview:
        render_brief_body(export_payload.brief, persisted=True)
    with decisions:
        render_reviewer_decisions(export_payload)
    with evidence:
        render_source_evidence(document, export_payload.brief)
    with details:
        render_run_details(export_payload)


def render_brief_metrics(
    brief: InspectionBrief, payload: VersionedBriefExport | None = None,
) -> None:
    """Keep brief size and saved-decision progress visible above the tabs."""
    with st.container(key="brief_metrics"):
        focus, gaps, evidence, reviewed = st.columns(4)
        focus.metric("Focus areas", len(brief.risk_domains))
        gaps.metric("Information gaps", len(information_gaps(brief)))
        evidence.metric("Source snippets", len(brief.evidence))
        total = len(brief.risk_domains) + len(brief.missing_info)
        saved = len({(d.target_type, d.target_index) for d in payload.reviewer_decisions}) if payload else 0
        reviewed.metric("Decisions saved", f"{saved} / {total}" if payload else "Preview only")


def render_source_evidence(document: dict, brief: InspectionBrief) -> None:
    """Put the evidence index beside its readable source excerpt."""
    with st.container(key="source_layout"):
        evidence, source = st.columns([1, 1], gap="large")
    with evidence:
        st.subheader("Evidence index", anchor=False)
        st.caption("Keyword matches with approximate source locations.")
        render_evidence_inspector(brief, key=f"evidence_match:{document['id']}")
    with source:
        st.subheader("Source excerpt", anchor=False)
        st.markdown(f"**Source:** {escape_markdown(document['source'])}")
        if document.get("source_url"):
            st.markdown(f"**Source URL:** {escape_markdown(document['source_url'])}")
        elif document["source"] == "synthetic_sample":
            st.caption("Synthetic test data. This is not a real public tender.")
        with st.container(height=360, border=True, key="source_text"):
            st.text(document["clean_text"])


def render_run_details(payload: VersionedBriefExport) -> None:
    """Separate reproducibility and validation details from everyday review."""
    with st.container(key="run_layout"):
        run, validation = st.columns([1, 1.2], gap="large")
    with run:
        st.subheader("Processing record", anchor=False)
        st.caption("Details of the saved brief and the rules used to generate it.")
        record = [
            f"**Brief:** #{payload.brief_id} · **Run:** #{payload.processing_run.id}",
            f"**Processed:** {payload.processing_run.processed_at:%Y-%m-%d %H:%M UTC}",
            f"**Extractor:** {payload.processing_run.extractor_name}",
            f"**Extractor version:** {payload.processing_run.extractor_version}",
            f"**Brief schema:** {payload.processing_run.brief_schema_version}",
            f"**Export schema:** {payload.export_schema_version}",
        ]
        st.markdown("  \n".join(record))
        st.markdown("**Source fingerprint · SHA-256**")
        st.code(payload.processing_run.source_content_fingerprint, language=None, wrap_lines=True)
        st.caption("Identifies the cleaned source text used for this brief.")
        st.caption("Reprocessing creates a new brief; earlier decisions stay with their original brief.")
        with st.expander("Original generated summary"):
            st.text(payload.brief.summary)
    with validation:
        st.subheader("Checks on bundled samples", anchor=False)
        render_validation_snapshot()


def render_validation_snapshot() -> None:
    """Render the committed qualitative comparison in the Run details view."""
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
                column_order=["sample_id", "source_type", "match_status"],
                column_config={
                    "sample_id": st.column_config.TextColumn("Sample", width="medium"),
                    "source_type": st.column_config.TextColumn(
                        "Source type", width="medium"
                    ),
                    "match_status": st.column_config.TextColumn("Match", width="small"),
                },
            )
        st.caption(
            'Here, "Match" means that the detected review topics agreed with '
            "the topics expected by a person reviewing that sample. These few "
            "examples do not show how accurately the rules will work on other text. Detailed notes "
            "are stored in `data/labels/manual_validation_v1.csv`."
        )


def _remember_adhoc_field(field: str) -> None:
    """Keep committed edits after Streamlit removes a hidden page's widgets."""
    draft = dict(st.session_state.get("adhoc_draft", {}))
    draft[field] = st.session_state[f"adhoc_{field}"]
    st.session_state["adhoc_draft"] = draft
    # A Generate click can arrive with field edits; its validation owns feedback.
    if not st.session_state.get("generate_preview", False):
        st.session_state["adhoc_input_invalid"] = False
        st.session_state.pop("adhoc_error", None)


def _generate_adhoc_preview() -> None:
    """Generate only after an explicit click, before rendering its action state."""
    inputs = {
        "text": st.session_state.get("adhoc_text", ""),
        "title": st.session_state.get("adhoc_title", ""),
        "url": st.session_state.get("adhoc_url", ""),
    }
    st.session_state["adhoc_draft"] = inputs.copy()
    error = adhoc_input_error(inputs["text"])
    st.session_state["adhoc_input_invalid"] = bool(error)
    st.session_state["adhoc_error"] = error
    if error:
        # Retain the last successful preview when a later submission is invalid.
        return
    document = build_document_from_text(
        inputs["text"], source_url=inputs["url"].strip(),
        default_title=inputs["title"].strip() or "Pasted public excerpt",
    )
    st.session_state["adhoc_preview"] = document, extract_brief(document)
    # Keep submitted fields separate from the draft so later edits can mark the preview as out of date.
    st.session_state["adhoc_preview_input"] = inputs.copy()
    request_scroll("preview")


def render_adhoc_view() -> None:
    """Keep input beside a persistent, session-only preview."""
    with st.container(key="excerpt_layout"):
        input_column, preview_column = st.columns([1, 1.15], gap="large")
    draft = st.session_state.get("adhoc_draft", {})
    for field in ("title", "url", "text"):
        key = f"adhoc_{field}"
        if key not in st.session_state:
            st.session_state[key] = draft.get(field, "")

    with input_column, st.container(border=True, key="excerpt_input"):
        st.subheader("Public excerpt", anchor=False)
        st.caption("Public, non-confidential text only · Up to 50,000 characters")
        # Widget callbacks retain field edits outside the temporary widget keys.
        # Restoring those fields does not run extraction; only Generate does that.
        text = st.text_area(
            "Paste a public tender or technical document excerpt",
            height=260, max_chars=MAX_ADHOC_CHARS, key="adhoc_text",
            placeholder="Paste the tender scope, technical requirements, or a public notice excerpt…",
            on_change=_remember_adhoc_field, args=("text",),
        )
        title = st.text_input(
            "Document title (optional)", key="adhoc_title",
            help=(
                "The app first looks for a title on a line such as TITLE: Roof works "
                "in the pasted text. If no such line contains a title, it uses this field."
            ),
            on_change=_remember_adhoc_field, args=("title",),
        )
        source_url = st.text_input(
            "Source URL (optional)", key="adhoc_url",
            help="Shown as a source reference. The app does not open or download the page.",
            on_change=_remember_adhoc_field, args=("url",),
        )
        inputs = {"title": title, "url": source_url, "text": text}
        has_preview = st.session_state.get("adhoc_preview") is not None
        preview_current = (
            has_preview and inputs == st.session_state.get("adhoc_preview_input")
            and not st.session_state.get("adhoc_input_invalid", False)
        )
        if preview_current:
            generate_label, generate_icon = "Generate again", ":material/check_circle:"
        elif has_preview:
            generate_label, generate_icon = "Update review brief", ":material/refresh:"
        else:
            generate_label, generate_icon = "Generate review brief", ":material/play_arrow:"
        st.button(
            generate_label, icon=generate_icon,
            type="secondary" if preview_current else "primary",
            width="stretch", key="generate_preview", on_click=_generate_adhoc_preview,
            help="Generate a new preview from the current input.",
        )
        if preview_current:
            st.badge("Review brief generated", icon=":material/check:", color="green")
        if error := st.session_state.get("adhoc_error"):
            st.warning(error)
        st.caption("Draft and preview stay in this browser session. Decisions and JSON downloads are available for bundled samples.")

    with preview_column, st.container(key="preview_result"):
        preview = st.session_state.get("adhoc_preview")
        if preview is None:
            with st.container(border=True, key="preview_empty"):
                st.subheader("Your brief will appear here", anchor=False)
                st.write("Paste a public excerpt and select Generate review brief.")
                st.caption("The preview includes matching source text and suggested questions to check.")
            return

        document, brief = preview
        st.subheader("Review brief preview", anchor=False)
        st.badge("Session only", icon=":material/visibility:", color="gray")
        st.text(document.title)
        if st.session_state.get("adhoc_input_invalid"):
            st.info("Your latest input was not analysed. Showing the last generated preview.")
        elif inputs != st.session_state.get("adhoc_preview_input"):
            st.info("Input has changed. Showing the previous preview until you generate again.")
        else:
            st.caption("From the last generated excerpt. Generate again to update this preview.")
        with st.container(key="preview_metrics"):
            focus, gaps, evidence = st.columns(3)
        focus.metric("Focus areas", len(brief.risk_domains))
        gaps.metric("Information gaps", len(information_gaps(brief)))
        evidence.metric("Source snippets", len(brief.evidence))
        findings, snippets, questions, source = st.tabs(
            ["Findings", "Evidence", "Questions", "Source"], key="preview_tabs",
        )
        with findings:
            st.markdown("**Detected technical scopes**")
            if brief.technical_scopes:
                _render_scope_tags(brief.technical_scopes)
            else:
                st.caption("No technical topics matched the keyword rules. Read the excerpt manually.")
            if brief.risk_domains != brief.technical_scopes:
                _bullets(brief.risk_domains, "No review focus areas detected.")
            _render_information_gaps(brief)
        with snippets:
            render_evidence_inspector(brief, key="preview_evidence_match")
        with questions:
            _bullets(brief.review_questions, "No questions generated. Read the source manually.")
        with source:
            if document.source_url:
                st.markdown(f"**Source URL:** {escape_markdown(document.source_url)}")
            with st.container(border=True, height=360, key="preview_source"):
                st.text(document.clean_text)
        st.caption("Keyword-based preview · Confidence label: Low (fixed) · Human review required. Sample checks do not show how accurate this preview is.")


def render() -> None:
    """Render one workflow at a time in a shared application shell."""
    st.set_page_config(
        page_title=APP_TITLE,
        page_icon=":material/fact_check:",
        layout="wide",
        initial_sidebar_state="auto",
    )
    st.html(Path(__file__).with_name("styles.css"))
    st.html(Path(__file__).with_name("navigation.css"))
    with st.sidebar:
        st.html(
            '<div class="app-brand"><div class="brand-mark">ti</div>'
            f'<div><div class="brand-name">{APP_TITLE}</div>'
            '<div class="brand-caption">Source to decision</div></div></div>'
        )
        page = st.radio(
            "Workspace navigation",
            ["Review workspace", "Analyse a public excerpt"],
            key="workspace_nav",
            label_visibility="collapsed",
        )

    with st.container(horizontal=True, horizontal_alignment="distribute", vertical_alignment="center", key="page_header"):
        with st.container(key="workspace_heading", width="content", gap=None):
            st.caption(APP_TITLE)
            st.title(page, width="content", anchor=False)
        if st.button("How it works", icon=":material/help_outline:", key="open_guide"):
            show_workflow_guide()
    if page == "Review workspace":
        render_bundled_view()
    else:
        st.caption("Paste public tender text to generate findings you can check against the source.")
        render_adhoc_view()

    with st.sidebar, st.container(key="sidebar_footer"):
        st.divider()
        st.caption("Keyword-based extraction")
        st.caption("Supports human technical review. Findings require verification.")

    render_pending_scroll()


if __name__ == "__main__":
    render()
