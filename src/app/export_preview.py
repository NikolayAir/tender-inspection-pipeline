"""Read-only inspection of the saved payload offered by the JSON download."""

import streamlit as st

from src.ai.risk_extract import NO_MATCH_NOTICE
from src.app.review import brief_findings, information_gaps, latest_decisions
from src.exports import VersionedBriefExport

_STATE_LABELS = {
    "accepted": "Accepted",
    "rejected": "Rejected",
    "needs_follow_up": "Needs follow-up",
}


def _items(values: list[str], empty: str) -> None:
    if values:
        with st.container(gap="small"):
            for value in values:
                st.write(f"• {value}")
    else:
        st.caption(empty)


@st.dialog("JSON download preview", width="large")
def show_export_preview(
    payload: VersionedBriefExport, json_text: str, filename: str,
) -> None:
    """Inspect one captured export without reading storage or changing a form."""
    brief = payload.brief
    with st.container(key="export_preview_intro", gap="small"):
        st.caption("Saved data only. Unsaved decisions and notes are excluded.")
        st.subheader(payload.document.title, anchor=False)
        st.caption(
            f"Focus areas: {len(brief.risk_domains)} · "
            f"Information gaps: {len(information_gaps(brief))} · "
            f"Saved history entries: {len(payload.reviewer_decisions)}"
        )
    st.download_button(
        "Download this JSON", data=json_text, file_name=filename,
        mime="application/json", on_click="ignore", icon=":material/download:",
        key="download_preview_file",
    )
    generated, evidence, decisions, details = st.tabs(
        ["Brief", "Source evidence", "Saved decisions", "File details"],
    )
    with generated:
        st.caption("Generated topics and follow-up questions to verify against the tender documentation.")
        if brief.technical_scopes == brief.risk_domains:
            st.markdown("**Technical scopes and review focus areas**")
            _items(brief.risk_domains, "No technical topics matched the keyword rules.")
        else:
            st.markdown("**Detected technical scopes**")
            _items(brief.technical_scopes, "No technical topics matched the keyword rules.")
            st.markdown("**Review focus areas**")
            _items(brief.risk_domains, "No review focus areas were generated.")
        st.markdown("**Information gaps**")
        _items(information_gaps(brief), "No explicit phrases about missing information matched.")
        if NO_MATCH_NOTICE in brief.missing_info:
            st.info(NO_MATCH_NOTICE)
        st.markdown("**Questions to check**")
        _items(brief.review_questions, "No questions were generated.")
        st.caption(
            f"Confidence label: {brief.confidence} · "
            f"Human review required: {'Yes' if brief.human_review_required else 'No'}. "
            "For the current keyword rules, this is a fixed label, not an accuracy score."
        )
        with st.expander("Original generated summary"):
            st.write(brief.summary)
    with evidence, st.container(key="export_preview_evidence"):
        st.caption("These passages are included in the file. Locations refer to the cleaned excerpt and are approximate.")
        for index, snippet in enumerate(brief.evidence, start=1):
            with st.container(border=True):
                st.caption(f'{index} · Matched term: "{snippet.matched_term}" · {snippet.location}')
                st.text(snippet.snippet)
        if not brief.evidence:
            st.info("No supporting source passages were generated. Read the excerpt manually.")
    with decisions, st.container(key="export_preview_decisions"):
        findings = brief_findings(brief)
        latest = latest_decisions(payload)
        labels = {finding.identity: finding.value for finding in findings}
        st.caption("Latest saved decision for each finding. Unreviewed means no decision has been saved.")
        for finding in findings:
            current = latest.get(finding.identity)
            with st.container(border=True):
                st.caption(finding.kind)
                st.write(finding.value)
                st.write(_STATE_LABELS[current.state] if current else "Unreviewed")
                if current:
                    st.text(current.note or "No note provided")
        if not findings:
            st.info("This brief contains no findings to review.")
        with st.expander(f"Complete decision history ({len(payload.reviewer_decisions)})"):
            if not payload.reviewer_decisions:
                st.caption("No decisions have been saved for this brief.")
            else:
                st.caption("All saved events, from earliest to latest. Saving again adds another event.")
                with st.container(height=360):
                    for event in payload.reviewer_decisions:
                        identity = event.target_type, event.target_index
                        finding_label = labels.get(identity, f"{event.target_type}[{event.target_index}]")
                        st.write(f"{_STATE_LABELS[event.state]} · {finding_label}")
                        st.caption(f"Event {event.id} · {event.decided_at:%Y-%m-%d %H:%M:%S} UTC")
                        st.text(event.note or "No note provided")
                        st.divider()
    with details:
        run = payload.processing_run
        st.markdown("**File and document**")
        st.text(
            f"Filename: {filename}\n"
            f"Export format version: {payload.export_schema_version}\n"
            f"Brief ID: {payload.brief_id}\n"
            f"Document ID: {payload.document.id}\n"
            f"Source: {payload.document.source}\n"
            f"Source URL: {payload.document.source_url or 'Not recorded'}"
        )
        st.markdown("**Processing record**")
        st.text(
            f"Run ID: {run.id}\n"
            f"Processed: {run.processed_at:%Y-%m-%d %H:%M:%S} UTC\n"
            f"Extractor: {run.extractor_name} {run.extractor_version}\n"
            f"Brief format version: {run.brief_schema_version}"
        )
        st.caption("Source fingerprint (SHA-256 of the cleaned text):")
        st.code(run.source_content_fingerprint, language=None, wrap_lines=True)
        st.caption("The file includes the quoted passages, not the full source excerpt. Read the full excerpt under Evidence & source in the workspace. The fingerprint identifies text; it does not archive it.")
    with st.expander("Exact JSON"):
        st.caption("Exact contents of Download this JSON, with lines wrapped for reading.")
        st.code(json_text, language="json", wrap_lines=True, height=360)


@st.fragment
def render_export_actions(
    payload: VersionedBriefExport, json_text: str, filename: str,
) -> None:
    """Keep inspection outside the decision form and on its displayed snapshot."""
    if st.button(
        "Preview JSON", icon=":material/visibility:",
        key="preview_export", width="stretch",
        help="Inspect the saved brief and decision history before downloading.",
    ):
        show_export_preview(payload, json_text, filename)
    # Download these exact bytes without a rerun that could disturb the review draft.
    st.download_button(
        "Download review brief (JSON)", data=json_text,
        file_name=filename, mime="application/json", on_click="ignore",
        icon=":material/download:", width="stretch",
        help=(
            "Includes the generated brief, supporting source passages, "
            "document and processing details, and saved decision history. "
            "Unsaved form changes are excluded."
        ),
    )
