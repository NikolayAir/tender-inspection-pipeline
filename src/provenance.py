"""Processing-run provenance for persisted inspection briefs."""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone

from src.models import ProcessingRun, TenderDocument

EXTRACTOR_NAME = "deterministic_keyword_baseline"
EXTRACTOR_VERSION = "1.0.1"
BRIEF_SCHEMA_VERSION = "1.0.0"

LEGACY_EXTRACTOR_NAME = "legacy_unversioned"
LEGACY_EXTRACTOR_VERSION = "unknown"
LEGACY_BRIEF_SCHEMA_VERSION = "legacy_unversioned"


def source_content_fingerprint(clean_text: str) -> str:
    """Return a deterministic SHA-256 fingerprint of normalised source text."""
    return hashlib.sha256(clean_text.encode("utf-8")).hexdigest()


def build_processing_run(
    document_id: int,
    document: TenderDocument,
    *,
    processed_at: datetime | None = None,
) -> ProcessingRun:
    """Build current-version provenance metadata for one processing execution."""
    timestamp = processed_at or datetime.now(timezone.utc)
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ValueError("processed_at must be timezone-aware")

    return ProcessingRun(
        document_id=document_id,
        processed_at=timestamp.astimezone(timezone.utc),
        extractor_name=EXTRACTOR_NAME,
        extractor_version=EXTRACTOR_VERSION,
        brief_schema_version=BRIEF_SCHEMA_VERSION,
        source_content_fingerprint=source_content_fingerprint(document.clean_text),
    )
