"""Shared Pydantic models for Tender-to-Inspection Brief.

These models are shared across the storage, extraction, and UI layers so the
structured source document and inspection-preparation brief use one schema.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field, field_validator


class TenderDocument(BaseModel):
    """A single tender/public-notice document used by the pipeline."""

    source: str = Field(description="Where the document came from, e.g. 'synthetic_sample'.")
    source_url: str | None = Field(
        default=None, description="Public URL if available; None for offline samples."
    )
    title: str = Field(description="Short human-readable project/document title.")
    raw_text: str = Field(description="Text retained before normalisation.")
    clean_text: str = Field(description="Whitespace/encoding-normalised text.")


class ProcessingRun(BaseModel):
    """Auditable metadata for one persisted document-processing execution."""

    document_id: int = Field(description="Stored source-document identifier.")
    processed_at: datetime = Field(description="Timezone-aware UTC processing timestamp.")
    extractor_name: str = Field(description="Stable identifier for the extraction method.")
    extractor_version: str = Field(description="Version of the extraction implementation.")
    brief_schema_version: str = Field(description="Version of the structured brief schema.")
    source_content_fingerprint: str = Field(
        description="SHA-256 fingerprint of the normalised source text."
    )


ReviewTargetType = Literal["risk_domain", "missing_info"]
ReviewState = Literal["accepted", "rejected", "needs_follow_up"]


class ReviewerDecision(BaseModel):
    """One human-authored decision event for a generated brief item."""

    brief_id: int = Field(
        gt=0,
        description="Persisted inspection-brief identifier.",
    )
    target_type: ReviewTargetType = Field(
        description="Generated brief collection containing the review target."
    )
    target_index: int = Field(
        ge=0,
        description="Zero-based target position within the linked immutable brief.",
    )
    state: ReviewState = Field(
        description="Reviewer-selected state for the target."
    )
    note: str | None = Field(
        default=None,
        max_length=2000,
        description="Optional reviewer note, limited to 2,000 characters.",
    )
    decided_at: datetime = Field(
        description="Timezone-aware reviewer-decision timestamp normalised to UTC."
    )

    @field_validator("note", mode="before")
    @classmethod
    def normalize_note(cls, value: object) -> object:
        """Trim reviewer notes and store blank notes as None."""
        if value is None or not isinstance(value, str):
            return value

        normalized = value.strip()
        return normalized or None

    @field_validator("decided_at")
    @classmethod
    def normalize_decided_at(cls, value: datetime) -> datetime:
        """Require an aware timestamp and normalize it to UTC."""
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("decided_at must be timezone-aware")

        return value.astimezone(timezone.utc)


class StoredReviewerDecision(ReviewerDecision):
    """Persisted reviewer-decision event with its database identifier."""

    id: int = Field(
        gt=0,
        description="Stored reviewer-decision event identifier.",
    )


class EvidenceSnippet(BaseModel):
    """A source passage supporting a keyword or information-gap phrase match.

    The matched term and approximate location explain why the passage was
    included. A no-match manual-review notice has no supporting passage.
    """

    snippet: str = Field(description="Short quoted text from the source.")
    matched_term: str = Field(description="Keyword or phrase that triggered the match.")
    location: str = Field(
        description="Approximate location in the normalised excerpt, e.g. 'line 12' or 'source text'."
    )


class InspectionBrief(BaseModel):
    """The evidence-based inspection-preparation brief for one document.

    This supports human technical review only. It is not a compliance, legal,
    safety, regulatory, or engineering decision.
    """

    summary: str = Field(description="Short, neutral project summary.")
    technical_scopes: list[str] = Field(
        default_factory=list, description="Detected technical scopes."
    )
    risk_domains: list[str] = Field(
        default_factory=list,
        description="Potential technical review domains for reviewer follow-up.",
    )
    missing_info: list[str] = Field(
        default_factory=list,
        description=(
            "Sentences matching configured information-gap phrases. For compatibility, "
            "this collection also holds the manual-review notice when no topic or gap matches."
        ),
    )
    review_questions: list[str] = Field(
        default_factory=list, description="Suggested questions for the reviewer."
    )
    evidence: list[EvidenceSnippet] = Field(
        default_factory=list, description="Source snippets backing the findings."
    )
    confidence: str = Field(
        default="low",
        description="Qualitative confidence; set to 'low' by the current rule-based baseline.",
    )
    human_review_required: bool = Field(
        default=True,
        description="Set to True by the current extraction path; output assists humans, it does not decide.",
    )
