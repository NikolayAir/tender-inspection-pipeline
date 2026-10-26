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
from pathlib import Path

# Allow `streamlit run src/app/streamlit_app.py` to import the `src` package by
# ensuring the repository root is on the path.
ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import streamlit as st  # noqa: E402

from src.ai.risk_extract import extract_brief  # noqa: E402
from src.app.review import (  # noqa: E402
    REVIEW_FILTERS,
    brief_findings,
    category_for_term,
    evidence_for_finding,
    filter_findings,
    latest_decisions,
)
from src.collect.sample_loader import build_document_from_text  # noqa: E402
from src.db import database  # noqa: E402
from src.exports import VersionedBriefExport, serialize_brief_export  # noqa: E402
from src.models import (  # noqa: E402
    EvidenceSnippet,
    InspectionBrief,
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


def render_brief_body(brief: InspectionBrief) -> None:
    """Show a compact overview without duplicating scopes and focus areas."""
    summary, questions = st.columns([1.1, 1], gap="large")
    with summary, st.container(border=True, key="overview_summary"):
        st.subheader("At a glance", anchor=False)
        st.write(brief.summary)
        st.markdown("**Detected technical scopes**")
        if brief.technical_scopes:
            tags = "".join(
                f'<span class="scope-tag">{escape(scope)}</span>'
                for scope in brief.technical_scopes
            )
            st.html(f'<div class="scope-tags">{tags}</div>')
        else:
            st.caption("No technical scopes matched. Read the source manually.")
        if brief.risk_domains != brief.technical_scopes:
            st.markdown("**Review focus areas**")
            _bullets(brief.risk_domains, "No review focus areas detected.")
        st.markdown("**Information gaps**")
        _bullets(
            brief.missing_info,
            "No explicit gaps matched. This does not establish completeness.",
        )
    with questions:
        st.subheader("Questions to take into review", anchor=False)
        st.caption("Suggested prompts to check against the full tender documentation.")
        if brief.review_questions:
            for index, question in enumerate(brief.review_questions, 1):
                st.markdown(f"**{index:02d}** · {question}")
        else:
            st.write("No questions generated. Start by reading the source excerpt.")


def render_evidence_table(brief: InspectionBrief) -> None:
    """Show evidence separately from the overview and decision forms."""
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
        st.write(generated_value)

    if current is None:
        st.write("**Current review status:**", "Unreviewed")
        if show_finding:
            st.caption("No reviewer decision has been saved for this finding.")
    else:
        st.write("**Current review status:**", review_state_label(current.state))
        if show_finding:
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
            height=110,
            key=reviewer_widget_key(
                brief_id, target_type, target_index, "note"
            ),
            help="Saved notes are human-authored and limited to 2,000 characters.",
        )
        submitted = st.form_submit_button("Save decision", type="primary", width="stretch")

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


def _move_finding(key: str, identities: list[tuple], offset: int) -> None:
    """Move within the visible queue without changing persisted target indices."""
    index = identities.index(st.session_state[key])
    st.session_state[key] = identities[index + offset]


def render_reviewer_decisions(payload: VersionedBriefExport) -> None:
    """Review one original finding beside its supporting source evidence."""
    st.caption("Check the source on the left and record your decision on the right. Save before switching findings.")
    findings = brief_findings(payload.brief)
    if not findings:
        st.info("This brief contains no generated findings that require decisions.")
        return

    latest = latest_decisions(payload)
    history = [
        StoredReviewerDecision(brief_id=payload.brief_id, **event.model_dump())
        for event in payload.reviewer_decisions
    ]
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
    source, decision = st.columns([1.15, 1], gap="large")
    with source, st.container(border=True, key="finding_evidence"):
        st.badge(finding.kind, color="gray")
        st.subheader(finding.value, anchor=False)
        st.markdown("**Supporting source evidence**")
        snippets = evidence_for_finding(payload.brief, finding)
        for snippet in snippets:
            st.caption(f'Matched term: "{snippet.matched_term}" · {snippet.location} (approximate)')
            st.text(snippet.snippet)
        if not snippets:
            st.info("No direct supporting snippet was captured for this finding. Read the full source excerpt before deciding.")
        st.caption("A keyword match identifies a review topic; it does not establish a defect or compliance result.")
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

    if export_payload is None:
        st.info("No brief stored for this document.")
        return

    with st.container(key="brief_header"):
        heading, actions = st.columns([3, 1], vertical_alignment="center")
        with heading:
            st.subheader(document["title"], anchor=False)
            st.badge(
                "Synthetic test sample" if document["source"] == "synthetic_sample" else "Public tender excerpt",
                icon=":material/science:" if document["source"] == "synthetic_sample" else ":material/public:",
                color="gray",
            )
        with actions:
            st.download_button(
                "Download review brief (JSON)",
                data=serialize_brief_export(export_payload),
                file_name=brief_export_filename(document["id"]),
                mime="application/json",
                icon=":material/download:",
                width="stretch",
                help=(
                    "Includes the review brief, source evidence, document metadata, "
                    "processing time and versions, and the source fingerprint."
                ),
            )

    render_brief_metrics(export_payload.brief, export_payload)
    st.caption(
        "Human review required · Keyword-based findings · Baseline confidence: Low. "
        "Verify findings against the source before using them."
    )
    overview, decisions, evidence, details = st.tabs(
        ["Overview", "Review findings", "Evidence & source", "Run details"],
        key="brief_tabs",
        on_change="rerun",
    )
    with overview:
        render_brief_body(export_payload.brief)
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
        gaps.metric("Information gaps", len(brief.missing_info))
        evidence.metric("Source snippets", len(brief.evidence))
        total = len(brief.risk_domains) + len(brief.missing_info)
        saved = len({(d.target_type, d.target_index) for d in payload.reviewer_decisions}) if payload else 0
        reviewed.metric("Decisions saved", f"{saved} / {total}" if payload else "Preview only")


def render_source_evidence(document: dict, brief: InspectionBrief) -> None:
    """Put the evidence index beside its readable source excerpt."""
    evidence, source = st.columns([1, 1], gap="large")
    with evidence:
        st.subheader("Evidence index", anchor=False)
        st.caption("Keyword matches with approximate source locations.")
        render_evidence_table(brief)
    with source:
        st.subheader("Source excerpt", anchor=False)
        st.write(f"**Source:** {document['source']}")
        if document.get("source_url"):
            st.write(f"**Source URL:** {document['source_url']}")
        elif document["source"] == "synthetic_sample":
            st.caption("Synthetic test data. This is not a real public tender.")
        with st.container(height=360, border=True, key="source_text"):
            st.text(document["clean_text"])


def render_run_details(payload: VersionedBriefExport) -> None:
    """Separate reproducibility and validation details from everyday review."""
    run, validation = st.columns([1, 1.2], gap="large")
    with run:
        st.subheader("Processing record", anchor=False)
        st.write(f"**Brief:** #{payload.brief_id} · **Run:** #{payload.processing_run.id}")
        st.write(f"**Processed:** {payload.processing_run.processed_at:%Y-%m-%d %H:%M UTC}")
        st.write(f"**Extractor:** {payload.processing_run.extractor_name}")
        st.write(f"**Extractor version:** {payload.processing_run.extractor_version}")
        st.write(f"**Brief schema:** {payload.processing_run.brief_schema_version}")
        st.write(f"**Export schema:** {payload.export_schema_version}")
        st.markdown("**Source fingerprint · SHA-256**")
        st.code(payload.processing_run.source_content_fingerprint, language=None, wrap_lines=True)
        st.caption("Decisions belong to this brief. Reprocessing creates a new brief with its own review history.")
    with validation:
        st.subheader("Validation snapshot", anchor=False)
        render_validation_snapshot()


def render_validation_snapshot() -> None:
    """Render the committed qualitative comparison without hiding it in an accordion."""
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
    """Render one workflow at a time in a shared application shell."""
    st.set_page_config(
        page_title="Tender-to-Inspection Brief",
        page_icon=":material/fact_check:",
        layout="wide",
    )
    st.html(Path(__file__).with_name("styles.css"))
    with st.sidebar:
        st.html(
            '<div class="app-brand"><div class="brand-mark">ti</div>'
            '<div><div class="brand-name">Tender inspection</div>'
            '<div class="brand-caption">Source to decision</div></div></div>'
        )
        page = st.radio(
            "Workspace navigation",
            ["Review workspace", "Analyse a public excerpt"],
            key="workspace_nav",
            label_visibility="collapsed",
        )

    st.html('<div class="eyebrow">Tender-to-Inspection Brief</div>')
    if page == "Review workspace":
        st.title("Review workspace")
        st.caption("Understand the scope. Check the evidence. Record your decisions.")
        render_bundled_view()
    else:
        st.title("Analyse a public excerpt")
        st.caption("Turn public tender text into a source-traced review preview.")
        render_adhoc_view()

    with st.sidebar, st.container(key="sidebar_footer"):
        st.divider()
        st.caption("Transparent keyword rules · No external AI service")
        st.caption("Supports human technical review. Findings require verification.")


if __name__ == "__main__":
    render()
