"""Display-only finding identities, review queues, and direct evidence links."""

from __future__ import annotations

from dataclasses import dataclass

from src.ai.risk_extract import KEYWORD_DOMAINS, MISSING_INFO_PHRASES
from src.exports import VersionedBriefExport
from src.models import (
    EvidenceSnippet,
    InspectionBrief,
    ReviewTargetType,
    StoredReviewerDecision,
)

REVIEW_FILTERS = {
    "All findings": None,
    "Unreviewed": "unreviewed",
    "Needs follow-up": "needs_follow_up",
    "Accepted": "accepted",
    "Rejected": "rejected",
}


@dataclass(frozen=True)
class Finding:
    """Keep a finding's original persisted identity when its queue is filtered."""

    target_type: ReviewTargetType
    target_index: int
    value: str

    @property
    def identity(self) -> tuple[ReviewTargetType, int]:
        return self.target_type, self.target_index

    @property
    def kind(self) -> str:
        return "Focus area" if self.target_type == "risk_domain" else "Information gap"


def category_for_term(term: str) -> str:
    """Translate an existing keyword match without changing extraction rules."""
    normalized = term.lower().strip()
    if normalized in {phrase.lower().strip() for phrase in MISSING_INFO_PHRASES}:
        return "Missing information"
    for domain, keywords in KEYWORD_DOMAINS.items():
        if normalized in {keyword.lower().strip() for keyword in keywords}:
            return domain
    return "Other"


def brief_findings(brief: InspectionBrief) -> list[Finding]:
    """Flatten generated findings while retaining their original collection index."""
    return [
        Finding(target_type, index, value)
        for target_type, values in (
            ("risk_domain", brief.risk_domains),
            ("missing_info", brief.missing_info),
        )
        for index, value in enumerate(values)
    ]


def latest_decisions(
    payload: VersionedBriefExport,
) -> dict[tuple[ReviewTargetType, int], StoredReviewerDecision]:
    """Use only the latest event from the same brief snapshot as the export."""
    # Events are ordered by ID, so the last entry for a target is its current decision.
    return {
        (event.target_type, event.target_index): StoredReviewerDecision(
            brief_id=payload.brief_id, **event.model_dump(),
        )
        for event in payload.reviewer_decisions
    }


def filter_findings(
    findings: list[Finding],
    latest: dict[tuple[ReviewTargetType, int], StoredReviewerDecision],
    filter_label: str,
) -> list[Finding]:
    """Filter by current state rather than by any earlier decision event."""
    state = REVIEW_FILTERS[filter_label]
    return [
        finding
        for finding in findings
        if state is None
        or (latest[finding.identity].state if finding.identity in latest else "unreviewed") == state
    ]


def evidence_for_finding(brief: InspectionBrief, finding: Finding) -> list[EvidenceSnippet]:
    """Link domain matches or the exact gap sentence, never unrelated gap evidence."""
    if finding.target_type == "risk_domain":
        return [item for item in brief.evidence if category_for_term(item.matched_term) == finding.value]
    # Match the gap's whole sentence after normalising whitespace and case;
    # a shared trigger phrase is not enough to associate a quotation with it.
    value = " ".join(finding.value.split()).casefold()
    return [
        item for item in brief.evidence
        if category_for_term(item.matched_term) == "Missing information"
        and " ".join(item.snippet.split()).casefold() == value
    ]
