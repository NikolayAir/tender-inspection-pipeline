"""Versioned JSON export contracts for persisted inspection briefs."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from src.models import InspectionBrief, ReviewState, ReviewTargetType

# File-format versions evolve independently of extractor and generated-brief versions.
BRIEF_EXPORT_SCHEMA_VERSION_V1_0 = "1.0.0"
BRIEF_EXPORT_SCHEMA_VERSION = "1.1.0"


class ExportDocument(BaseModel):
    """Document metadata included in a brief export."""

    id: int = Field(gt=0, description="Stored document identifier.")
    source: str = Field(description="Source label stored with the document.")
    source_url: str | None = Field(
        default=None,
        description="Public source URL when available.",
    )
    title: str = Field(description="Human-readable document title.")


class ExportProcessingRun(BaseModel):
    """Processing metadata identifying how the exported brief was produced."""

    id: int = Field(gt=0, description="Stored processing-run identifier.")
    processed_at: datetime = Field(
        description="Processing timestamp normalised to UTC."
    )
    extractor_name: str = Field(description="Stable extraction-method identifier.")
    extractor_version: str = Field(description="Extraction implementation version.")
    brief_schema_version: str = Field(
        description="Structured inspection-brief schema version."
    )
    source_content_fingerprint: str = Field(
        pattern=r"^[0-9a-f]{64}$",
        description="SHA-256 fingerprint of the normalised source text.",
    )

    @field_validator("processed_at")
    @classmethod
    def normalize_processed_at(cls, value: datetime) -> datetime:
        """Require an aware timestamp and normalise it to UTC."""
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("processed_at must be timezone-aware")
        return value.astimezone(timezone.utc)


class ExportReviewerDecision(BaseModel):
    """One append-only event recording a reviewer decision."""

    id: int = Field(
        gt=0,
        description="Stored reviewer-decision event identifier.",
    )
    target_type: ReviewTargetType = Field(
        description="Kind of generated brief item reviewed by this event."
    )
    target_index: int = Field(
        ge=0,
        description="Zero-based position of the reviewed item in its brief list.",
    )
    state: ReviewState = Field(
        description="Reviewer-selected state recorded by this event."
    )
    note: str | None = Field(
        default=None,
        max_length=2000,
        description="Optional reviewer note, limited to 2,000 characters.",
    )
    decided_at: datetime = Field(
        description="Time when the decision was recorded, normalised to UTC."
    )

    @field_validator("note", mode="before")
    @classmethod
    def normalize_note(cls, value: object) -> object:
        """Trim reviewer notes and represent blank notes as None."""
        if value is None or not isinstance(value, str):
            return value

        normalized = value.strip()
        return normalized or None

    @field_validator("decided_at")
    @classmethod
    def normalize_decided_at(cls, value: datetime) -> datetime:
        """Require an aware timestamp and normalise it to UTC."""
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("decided_at must be timezone-aware")
        return value.astimezone(timezone.utc)


class _BriefExportBase(BaseModel):
    """Fields shared by supported brief-export schema versions."""

    document: ExportDocument
    processing_run: ExportProcessingRun
    brief_id: int = Field(
        gt=0,
        description="Stored inspection-brief identifier.",
    )
    brief: InspectionBrief


class VersionedBriefExportV1_0(_BriefExportBase):
    """Schema 1.0.0 export retained for explicit compatibility serialisation."""

    export_schema_version: Literal["1.0.0"] = (
        BRIEF_EXPORT_SCHEMA_VERSION_V1_0
    )


class VersionedBriefExport(_BriefExportBase):
    """Current schema 1.1.0 export with reviewer-decision history."""

    export_schema_version: Literal["1.1.0"] = BRIEF_EXPORT_SCHEMA_VERSION
    reviewer_decisions: list[ExportReviewerDecision] = Field(
        default_factory=list,
        description=(
            "Full append-only decision history for this brief, "
            "ordered by ascending event ID."
        ),
    )

    @field_validator("reviewer_decisions")
    @classmethod
    def require_reviewer_decision_order(
        cls,
        value: list[ExportReviewerDecision],
    ) -> list[ExportReviewerDecision]:
        """Require unique event IDs in ascending insertion order."""
        # IDs define append order even when an event carries an earlier timestamp.
        if any(
            earlier.id >= later.id
            for earlier, later in zip(value, value[1:])
        ):
            raise ValueError(
                "reviewer_decisions must be ordered by strictly increasing id"
            )
        return value


BriefExportPayload = VersionedBriefExportV1_0 | VersionedBriefExport


def serialize_brief_export(payload: BriefExportPayload) -> str:
    """Serialise a supported export deterministically as JSON text."""
    # Fixed formatting and a final newline keep repeated exports of one payload byte-identical.
    return (
        json.dumps(
            payload.model_dump(mode="json"),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )
