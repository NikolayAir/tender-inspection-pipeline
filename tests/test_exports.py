"""Focused tests for the versioned inspection-brief JSON export contract."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from src.db import database
from src.exports import (
    BRIEF_EXPORT_SCHEMA_VERSION,
    BRIEF_EXPORT_SCHEMA_VERSION_V1_0,
    ExportDocument,
    ExportProcessingRun,
    ExportReviewerDecision,
    VersionedBriefExport,
    VersionedBriefExportV1_0,
    serialize_brief_export,
)
from src.models import (
    EvidenceSnippet,
    InspectionBrief,
    ProcessingRun,
    ReviewerDecision,
    TenderDocument,
)
from src.provenance import (
    LEGACY_BRIEF_SCHEMA_VERSION,
    LEGACY_EXTRACTOR_NAME,
    LEGACY_EXTRACTOR_VERSION,
)


def _export_payload() -> VersionedBriefExport:
    return VersionedBriefExport(
        document=ExportDocument(
            id=7,
            source="public_sample",
            source_url="https://example.org/tender",
            title="Building renovation works",
        ),
        processing_run=ExportProcessingRun(
            id=11,
            processed_at=datetime(
                2026,
                8,
                1,
                20,
                30,
                tzinfo=timezone(timedelta(hours=2)),
            ),
            extractor_name="deterministic_keyword_baseline",
            extractor_version="1.0.0",
            brief_schema_version="1.0.0",
            source_content_fingerprint="a" * 64,
        ),
        brief_id=13,
        brief=InspectionBrief(
            summary="Renovation works requiring structured technical review.",
            technical_scopes=["Facade"],
            risk_domains=["Fire safety"],
            missing_info=["Drawings not attached."],
            review_questions=["Are fire-safety drawings available?"],
            evidence=[
                EvidenceSnippet(
                    snippet="Fire-safety works are included.",
                    matched_term="fire",
                    location="line 4",
                )
            ],
        ),
    )


def test_versioned_export_has_stable_structure_and_metadata() -> None:
    payload = _export_payload()
    exported = json.loads(serialize_brief_export(payload))

    assert exported["export_schema_version"] == BRIEF_EXPORT_SCHEMA_VERSION
    assert exported["document"] == {
        "id": 7,
        "source": "public_sample",
        "source_url": "https://example.org/tender",
        "title": "Building renovation works",
    }
    assert exported["processing_run"]["id"] == 11
    assert exported["processing_run"]["processed_at"] == "2026-08-01T18:30:00Z"
    assert exported["processing_run"]["source_content_fingerprint"] == "a" * 64
    assert exported["brief_id"] == 13
    assert exported["brief"]["risk_domains"] == ["Fire safety"]
    assert exported["brief"]["evidence"][0]["matched_term"] == "fire"
    assert exported["brief"]["human_review_required"] is True
    assert exported["reviewer_decisions"] == []


def test_v1_0_export_remains_serialisable_without_reviewer_decisions() -> None:
    current = _export_payload()
    legacy = VersionedBriefExportV1_0(
        document=current.document,
        processing_run=current.processing_run,
        brief_id=current.brief_id,
        brief=current.brief,
    )

    exported = json.loads(serialize_brief_export(legacy))

    assert (
        exported["export_schema_version"]
        == BRIEF_EXPORT_SCHEMA_VERSION_V1_0
    )
    assert "reviewer_decisions" not in exported


def test_v1_1_export_preserves_reviewer_decision_order_and_fields() -> None:
    current = _export_payload()
    payload = VersionedBriefExport(
        document=current.document,
        processing_run=current.processing_run,
        brief_id=current.brief_id,
        brief=current.brief,
        reviewer_decisions=[
            ExportReviewerDecision(
                id=21,
                target_type="risk_domain",
                target_index=0,
                state="needs_follow_up",
                note="  Confirm fire safety documentation.  ",
                decided_at=datetime(
                    2026,
                    8,
                    2,
                    14,
                    30,
                    tzinfo=timezone(timedelta(hours=2)),
                ),
            ),
            ExportReviewerDecision(
                id=22,
                target_type="risk_domain",
                target_index=0,
                state="accepted",
                note=None,
                decided_at=datetime(
                    2026,
                    8,
                    2,
                    13,
                    0,
                    tzinfo=timezone.utc,
                ),
            ),
        ],
    )

    exported = json.loads(serialize_brief_export(payload))

    assert exported["reviewer_decisions"] == [
        {
            "decided_at": "2026-08-02T12:30:00Z",
            "id": 21,
            "note": "Confirm fire safety documentation.",
            "state": "needs_follow_up",
            "target_index": 0,
            "target_type": "risk_domain",
        },
        {
            "decided_at": "2026-08-02T13:00:00Z",
            "id": 22,
            "note": None,
            "state": "accepted",
            "target_index": 0,
            "target_type": "risk_domain",
        },
    ]


def test_export_rejects_naive_reviewer_decision_timestamp() -> None:
    with pytest.raises(ValidationError, match="timezone-aware"):
        ExportReviewerDecision(
            id=21,
            target_type="risk_domain",
            target_index=0,
            state="accepted",
            decided_at=datetime(2026, 8, 2, 12, 30),
        )


def test_v1_1_export_rejects_out_of_order_reviewer_decisions() -> None:
    current = _export_payload()

    with pytest.raises(ValidationError, match="strictly increasing id"):
        VersionedBriefExport(
            document=current.document,
            processing_run=current.processing_run,
            brief_id=current.brief_id,
            brief=current.brief,
            reviewer_decisions=[
                ExportReviewerDecision(
                    id=22,
                    target_type="risk_domain",
                    target_index=0,
                    state="accepted",
                    decided_at=datetime(2026, 8, 2, 13, 0, tzinfo=timezone.utc),
                ),
                ExportReviewerDecision(
                    id=21,
                    target_type="risk_domain",
                    target_index=0,
                    state="needs_follow_up",
                    decided_at=datetime(2026, 8, 2, 12, 0, tzinfo=timezone.utc),
                ),
            ],
        )


def test_serialisation_is_byte_stable_for_same_payload() -> None:
    payload = _export_payload()

    first = serialize_brief_export(payload)
    second = serialize_brief_export(payload)

    assert first == second
    assert first.endswith("\n")
    assert "Building renovation works" in first


def test_processing_timestamp_is_normalised_to_utc() -> None:
    payload = _export_payload()

    assert payload.processing_run.processed_at == datetime(
        2026,
        8,
        1,
        18,
        30,
        tzinfo=timezone.utc,
    )


def test_export_rejects_naive_processing_timestamp() -> None:
    with pytest.raises(ValidationError, match="timezone-aware"):
        ExportProcessingRun(
            id=11,
            processed_at=datetime(2026, 8, 1, 18, 30),
            extractor_name="deterministic_keyword_baseline",
            extractor_version="1.0.0",
            brief_schema_version="1.0.0",
            source_content_fingerprint="a" * 64,
        )


def test_export_rejects_invalid_source_fingerprint() -> None:
    with pytest.raises(ValidationError):
        ExportProcessingRun(
            id=11,
            processed_at=datetime(2026, 8, 1, 18, 30, tzinfo=timezone.utc),
            extractor_name="deterministic_keyword_baseline",
            extractor_version="1.0.0",
            brief_schema_version="1.0.0",
            source_content_fingerprint="not-a-sha256-fingerprint",
        )


def test_latest_persisted_export_returns_none_without_brief(tmp_path) -> None:
    db_path = tmp_path / "empty_export.db"
    database.init_db(db_path)

    document_id = database.insert_document(
        TenderDocument(
            source="test_source",
            source_url=None,
            title="Document without brief",
            raw_text="Raw source text.",
            clean_text="Raw source text.",
        ),
        db_path,
    )

    assert database.get_latest_brief_export(document_id, db_path) is None


def test_latest_persisted_export_preserves_metadata_brief_and_decision_history(
    tmp_path,
) -> None:
    db_path = tmp_path / "persisted_export.db"
    database.init_db(db_path)

    document = TenderDocument(
        source="public_sample",
        source_url="https://example.org/persisted-tender",
        title="Persisted building tender",
        raw_text="Persisted source text.",
        clean_text="Persisted source text.",
    )
    document_id = database.insert_document(document, db_path)

    first_run = ProcessingRun(
        document_id=document_id,
        processed_at=datetime(2026, 8, 1, 16, 0, tzinfo=timezone.utc),
        extractor_name="deterministic_keyword_baseline",
        extractor_version="1.0.0-first",
        brief_schema_version="1.0.0",
        source_content_fingerprint="a" * 64,
    )
    _, first_brief_id = database.insert_processing_result(
        first_run,
        InspectionBrief(
            summary="First persisted brief.",
            risk_domains=["Earlier fire safety finding"],
        ),
        db_path,
    )

    second_run = ProcessingRun(
        document_id=document_id,
        processed_at=datetime(
            2026,
            8,
            1,
            20,
            30,
            tzinfo=timezone(timedelta(hours=2)),
        ),
        extractor_name="deterministic_keyword_baseline",
        extractor_version="1.0.0-second",
        brief_schema_version="1.0.0",
        source_content_fingerprint="b" * 64,
    )
    second_run_id, second_brief_id = database.insert_processing_result(
        second_run,
        InspectionBrief(
            summary="Second persisted brief.",
            technical_scopes=["Facade"],
            risk_domains=["Fire safety"],
            missing_info=["Drawings are not attached."],
            evidence=[
                EvidenceSnippet(
                    snippet="Facade repair is required.",
                    matched_term="facade",
                    location="line 3",
                )
            ],
        ),
        db_path,
    )

    first_latest_decision_id = database.insert_reviewer_decision(
        ReviewerDecision(
            brief_id=second_brief_id,
            target_type="risk_domain",
            target_index=0,
            state="needs_follow_up",
            note="  Confirm the referenced drawings.  ",
            decided_at=datetime(
                2026,
                8,
                2,
                10,
                30,
                tzinfo=timezone(timedelta(hours=2)),
            ),
        ),
        db_path,
    )

    # Interleave an older-brief event so filtering cannot rely on an ID range.
    older_brief_decision_id = database.insert_reviewer_decision(
        ReviewerDecision(
            brief_id=first_brief_id,
            target_type="risk_domain",
            target_index=0,
            state="accepted",
            decided_at=datetime(2026, 8, 2, 8, 45, tzinfo=timezone.utc),
        ),
        db_path,
    )

    # The earlier timestamp confirms that export order follows event ID.
    second_latest_decision_id = database.insert_reviewer_decision(
        ReviewerDecision(
            brief_id=second_brief_id,
            target_type="risk_domain",
            target_index=0,
            state="accepted",
            note="Drawings confirmed.",
            decided_at=datetime(2026, 8, 2, 8, 0, tzinfo=timezone.utc),
        ),
        db_path,
    )
    missing_info_decision_id = database.insert_reviewer_decision(
        ReviewerDecision(
            brief_id=second_brief_id,
            target_type="missing_info",
            target_index=0,
            state="rejected",
            decided_at=datetime(2026, 8, 2, 9, 0, tzinfo=timezone.utc),
        ),
        db_path,
    )

    exported = database.get_latest_brief_export(document_id, db_path)

    assert exported is not None
    assert exported.document.id == document_id
    assert exported.document.source == document.source
    assert exported.document.source_url == document.source_url
    assert exported.document.title == document.title
    assert exported.processing_run.id == second_run_id
    assert exported.processing_run.processed_at == datetime(
        2026,
        8,
        1,
        18,
        30,
        tzinfo=timezone.utc,
    )
    assert exported.processing_run.extractor_version == "1.0.0-second"
    assert exported.processing_run.source_content_fingerprint == "b" * 64
    assert exported.brief_id == second_brief_id
    assert exported.brief.summary == "Second persisted brief."
    assert exported.brief.evidence[0].matched_term == "facade"
    assert exported.export_schema_version == BRIEF_EXPORT_SCHEMA_VERSION

    exported_decision_ids = [
        decision.id
        for decision in exported.reviewer_decisions
    ]
    assert exported_decision_ids == [
        first_latest_decision_id,
        second_latest_decision_id,
        missing_info_decision_id,
    ]
    assert older_brief_decision_id not in exported_decision_ids
    assert [
        (
            decision.target_type,
            decision.state,
            decision.note,
            decision.decided_at,
        )
        for decision in exported.reviewer_decisions
    ] == [
        (
            "risk_domain",
            "needs_follow_up",
            "Confirm the referenced drawings.",
            datetime(2026, 8, 2, 8, 30, tzinfo=timezone.utc),
        ),
        (
            "risk_domain",
            "accepted",
            "Drawings confirmed.",
            datetime(2026, 8, 2, 8, 0, tzinfo=timezone.utc),
        ),
        (
            "missing_info",
            "rejected",
            None,
            datetime(2026, 8, 2, 9, 0, tzinfo=timezone.utc),
        ),
    ]

    latest_brief = database.get_brief_for_document(document_id, db_path)
    assert latest_brief is not None
    assert latest_brief.summary == exported.brief.summary

    first_serialization = serialize_brief_export(exported)
    second_serialization = serialize_brief_export(exported)

    assert first_serialization == second_serialization
    parsed = json.loads(first_serialization)
    assert parsed["processing_run"]["id"] == second_run_id
    assert parsed["brief_id"] == second_brief_id
    assert [
        decision["id"]
        for decision in parsed["reviewer_decisions"]
    ] == exported_decision_ids


def test_legacy_migrated_brief_can_be_exported(tmp_path) -> None:
    db_path = tmp_path / "legacy_export.db"

    with database.connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE documents (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                source      TEXT NOT NULL,
                source_url  TEXT,
                title       TEXT NOT NULL,
                raw_text    TEXT NOT NULL,
                clean_text  TEXT NOT NULL,
                created_at  TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE TABLE briefs (
                id                    INTEGER PRIMARY KEY AUTOINCREMENT,
                document_id           INTEGER NOT NULL REFERENCES documents(id),
                summary               TEXT NOT NULL,
                technical_scopes      TEXT NOT NULL DEFAULT '[]',
                risk_domains          TEXT NOT NULL DEFAULT '[]',
                missing_info          TEXT NOT NULL DEFAULT '[]',
                review_questions      TEXT NOT NULL DEFAULT '[]',
                evidence              TEXT NOT NULL DEFAULT '[]',
                confidence            TEXT NOT NULL DEFAULT 'low',
                human_review_required INTEGER NOT NULL DEFAULT 1,
                created_at            TEXT NOT NULL DEFAULT (datetime('now'))
            );
            """
        )

        document_cursor = conn.execute(
            """
            INSERT INTO documents (
                source,
                source_url,
                title,
                raw_text,
                clean_text
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                "legacy_source",
                None,
                "Legacy export tender",
                "Legacy raw text.",
                "Legacy clean text.",
            ),
        )
        assert document_cursor.lastrowid is not None
        document_id = document_cursor.lastrowid

        conn.execute(
            """
            INSERT INTO briefs (
                document_id,
                summary,
                created_at
            )
            VALUES (?, ?, ?)
            """,
            (
                document_id,
                "Legacy persisted brief.",
                "2026-06-04 12:00:00",
            ),
        )

    database.init_db(db_path)

    exported = database.get_latest_brief_export(document_id, db_path)

    assert exported is not None
    assert exported.processing_run.processed_at == datetime(
        2026,
        6,
        4,
        12,
        0,
        tzinfo=timezone.utc,
    )
    assert exported.processing_run.extractor_name == LEGACY_EXTRACTOR_NAME
    assert exported.processing_run.extractor_version == LEGACY_EXTRACTOR_VERSION
    assert (
        exported.processing_run.brief_schema_version
        == LEGACY_BRIEF_SCHEMA_VERSION
    )
    assert exported.brief.summary == "Legacy persisted brief."
    assert exported.reviewer_decisions == []
