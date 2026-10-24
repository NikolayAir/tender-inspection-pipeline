"""Focused tests for processing-run provenance metadata."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.models import TenderDocument
from src.provenance import (
    BRIEF_SCHEMA_VERSION,
    EXTRACTOR_NAME,
    EXTRACTOR_VERSION,
    build_processing_run,
    source_content_fingerprint,
)


def _document(clean_text: str) -> TenderDocument:
    return TenderDocument(
        source="test_source",
        source_url=None,
        title="Test tender",
        raw_text=clean_text,
        clean_text=clean_text,
    )


def test_source_content_fingerprint_is_deterministic() -> None:
    clean_text = "Normalized tender text."

    first = source_content_fingerprint(clean_text)
    second = source_content_fingerprint(clean_text)

    assert first == second
    assert len(first) == 64
    assert all(character in "0123456789abcdef" for character in first)


def test_source_content_fingerprint_changes_with_clean_text() -> None:
    first = source_content_fingerprint("Normalized tender text.")
    second = source_content_fingerprint("Normalized tender text changed.")

    assert first != second


def test_build_processing_run_records_versions_and_normalizes_utc() -> None:
    local_time = datetime(
        2026,
        7,
        31,
        22,
        0,
        tzinfo=timezone(timedelta(hours=2)),
    )
    document = _document("Normalized tender text.")

    run = build_processing_run(
        document_id=7,
        document=document,
        processed_at=local_time,
    )

    assert run.document_id == 7
    assert run.processed_at == datetime(2026, 7, 31, 20, 0, tzinfo=timezone.utc)
    assert run.processed_at.utcoffset() == timedelta(0)
    assert run.extractor_name == EXTRACTOR_NAME
    assert run.extractor_version == EXTRACTOR_VERSION
    assert run.brief_schema_version == BRIEF_SCHEMA_VERSION
    assert run.source_content_fingerprint == source_content_fingerprint(
        document.clean_text
    )


def test_build_processing_run_rejects_naive_timestamp() -> None:
    document = _document("Normalized tender text.")

    with pytest.raises(ValueError, match="timezone-aware"):
        build_processing_run(
            document_id=7,
            document=document,
            processed_at=datetime(2026, 7, 31, 20, 0),
        )


def test_new_database_schema_requires_processing_run_links(tmp_path) -> None:
    from src.db import database

    db_path = tmp_path / "new_schema.db"
    database.init_db(db_path)

    with database.connect(db_path) as conn:
        table_names = {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        brief_columns = {
            row["name"]: row
            for row in conn.execute("PRAGMA table_info(briefs)").fetchall()
        }
        brief_indexes = conn.execute(
            "PRAGMA index_list(briefs)"
        ).fetchall()
        brief_foreign_keys = conn.execute(
            "PRAGMA foreign_key_list(briefs)"
        ).fetchall()

    assert {"documents", "processing_runs", "briefs"}.issubset(table_names)
    assert brief_columns["processing_run_id"]["notnull"] == 1
    assert any(
        row["name"] == "idx_briefs_document_id"
        and row["unique"] == 0
        for row in brief_indexes
    )
    assert any(
        row["name"] == "idx_briefs_processing_run_id"
        and row["unique"] == 1
        for row in brief_indexes
    )
    assert any(
        row["from"] == "processing_run_id"
        and row["table"] == "processing_runs"
        and row["to"] == "id"
        for row in brief_foreign_keys
    )


def test_existing_database_is_backfilled_idempotently(tmp_path) -> None:
    from src.db import database
    from src.provenance import (
        LEGACY_BRIEF_SCHEMA_VERSION,
        LEGACY_EXTRACTOR_NAME,
        LEGACY_EXTRACTOR_VERSION,
    )

    db_path = tmp_path / "legacy.db"

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
                "Legacy tender",
                "Legacy raw text",
                "Legacy clean text",
            ),
        )
        assert document_cursor.lastrowid is not None
        document_id = document_cursor.lastrowid

        brief_cursor = conn.execute(
            """
            INSERT INTO briefs (
                document_id,
                summary,
                technical_scopes,
                risk_domains,
                missing_info,
                review_questions,
                evidence,
                confidence,
                human_review_required,
                created_at
            )
            VALUES (?, ?, '[]', '[]', '[]', '[]', '[]', 'low', 1, ?)
            """,
            (
                document_id,
                "Legacy summary",
                "2026-06-04 12:00:00",
            ),
        )
        assert brief_cursor.lastrowid is not None
        brief_id = brief_cursor.lastrowid

    database.init_db(db_path)
    database.init_db(db_path)

    with database.connect(db_path) as conn:
        document_count = conn.execute(
            "SELECT COUNT(*) AS count FROM documents"
        ).fetchone()["count"]
        brief_count = conn.execute(
            "SELECT COUNT(*) AS count FROM briefs"
        ).fetchone()["count"]
        run_count = conn.execute(
            "SELECT COUNT(*) AS count FROM processing_runs"
        ).fetchone()["count"]

        brief_row = conn.execute(
            "SELECT * FROM briefs WHERE id = ?",
            (brief_id,),
        ).fetchone()

        run_row = conn.execute(
            "SELECT * FROM processing_runs WHERE id = ?",
            (brief_row["processing_run_id"],),
        ).fetchone()

        foreign_key_violations = conn.execute(
            "PRAGMA foreign_key_check"
        ).fetchall()

    assert document_count == 1
    assert brief_count == 1
    assert run_count == 1

    assert brief_row["document_id"] == document_id
    assert brief_row["summary"] == "Legacy summary"
    assert brief_row["processing_run_id"] is not None

    assert run_row["document_id"] == document_id
    assert run_row["processed_at"] == "2026-06-04 12:00:00"
    assert run_row["extractor_name"] == LEGACY_EXTRACTOR_NAME
    assert run_row["extractor_version"] == LEGACY_EXTRACTOR_VERSION
    assert run_row["brief_schema_version"] == LEGACY_BRIEF_SCHEMA_VERSION
    assert run_row["source_content_fingerprint"] == source_content_fingerprint(
        "Legacy clean text"
    )
    assert foreign_key_violations == []


def test_processing_result_insert_is_atomic(tmp_path) -> None:
    import sqlite3

    from src.db import database
    from src.models import InspectionBrief
    from src.provenance import build_processing_run

    db_path = tmp_path / "atomic.db"
    database.init_db(db_path)

    document = _document("Atomic persistence text.")
    document_id = database.insert_document(document, db_path)
    processing_run = build_processing_run(document_id, document)
    brief = InspectionBrief(summary="Atomic persistence test.")

    with database.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TRIGGER fail_brief_insert
            BEFORE INSERT ON briefs
            BEGIN
                SELECT RAISE(ABORT, 'forced brief failure');
            END
            """
        )

    with pytest.raises(sqlite3.IntegrityError, match="forced brief failure"):
        database.insert_processing_result(
            processing_run,
            brief,
            db_path,
        )

    with database.connect(db_path) as conn:
        run_count = conn.execute(
            "SELECT COUNT(*) AS count FROM processing_runs"
        ).fetchone()["count"]
        brief_count = conn.execute(
            "SELECT COUNT(*) AS count FROM briefs"
        ).fetchone()["count"]

    assert run_count == 0
    assert brief_count == 0


def test_latest_brief_and_current_provenance_are_persisted(tmp_path) -> None:
    from src.db import database
    from src.models import InspectionBrief
    from src.provenance import (
        BRIEF_SCHEMA_VERSION,
        EXTRACTOR_NAME,
        EXTRACTOR_VERSION,
        build_processing_run,
    )

    db_path = tmp_path / "processing_history.db"
    database.init_db(db_path)

    document = _document("Persisted provenance text.")
    document_id = database.insert_document(document, db_path)

    first_run = build_processing_run(
        document_id,
        document,
        processed_at=datetime(2026, 7, 31, 18, 0, tzinfo=timezone.utc),
    )
    first_run_id, first_brief_id = database.insert_processing_result(
        first_run,
        InspectionBrief(summary="First persisted brief."),
        db_path,
    )

    second_run = build_processing_run(
        document_id,
        document,
        processed_at=datetime(2026, 7, 31, 19, 0, tzinfo=timezone.utc),
    )
    second_run_id, second_brief_id = database.insert_processing_result(
        second_run,
        InspectionBrief(summary="Second persisted brief."),
        db_path,
    )

    latest_brief = database.get_brief_for_document(document_id, db_path)

    with database.connect(db_path) as conn:
        run_rows = conn.execute(
            """
            SELECT *
            FROM processing_runs
            ORDER BY id
            """
        ).fetchall()
        brief_rows = conn.execute(
            """
            SELECT id, processing_run_id, summary
            FROM briefs
            ORDER BY id
            """
        ).fetchall()

    assert latest_brief is not None
    assert latest_brief.summary == "Second persisted brief."

    assert [row["id"] for row in run_rows] == [
        first_run_id,
        second_run_id,
    ]
    assert [row["id"] for row in brief_rows] == [
        first_brief_id,
        second_brief_id,
    ]
    assert [row["processing_run_id"] for row in brief_rows] == [
        first_run_id,
        second_run_id,
    ]

    assert all(row["extractor_name"] == EXTRACTOR_NAME for row in run_rows)
    assert all(row["extractor_version"] == EXTRACTOR_VERSION for row in run_rows)
    assert all(
        row["brief_schema_version"] == BRIEF_SCHEMA_VERSION
        for row in run_rows
    )
    assert all(
        row["source_content_fingerprint"]
        == source_content_fingerprint(document.clean_text)
        for row in run_rows
    )
    assert [row["processed_at"] for row in run_rows] == [
        "2026-07-31T18:00:00+00:00",
        "2026-07-31T19:00:00+00:00",
    ]
