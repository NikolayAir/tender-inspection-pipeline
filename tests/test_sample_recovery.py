"""Automatic sample recovery must preserve completed and reviewed results."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import datetime, timezone

import pytest

from src.app import streamlit_app
from src.collect.sample_loader import load_sample
from src.db import database
from src.exports import serialize_brief_export
from src.models import ReviewerDecision
from src.pipeline import (
    BUNDLED_SAMPLES,
    ensure_bundled_samples,
    ingest_bundled_samples,
    run_pipeline,
)
from src.provenance import LEGACY_EXTRACTOR_NAME, source_content_fingerprint


@pytest.fixture
def demo_db(tmp_path, monkeypatch):
    """Redirect the application's default database to a temporary file."""
    db_path = tmp_path / "demo.db"
    original_connect = database.connect
    monkeypatch.setattr(
        database, "connect",
        lambda _path=database.DEFAULT_DB_PATH: original_connect(db_path),
    )
    database.init_db(db_path)
    return db_path


def _snapshot(db_path):
    with closing(database.connect(db_path)) as conn:
        return {
            table: [dict(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY id")]
            for table in ("documents", "processing_runs", "briefs", "reviewer_decisions")
        }


def _assert_original_rows_retained(original, db_path):
    after = _snapshot(db_path)
    for table, rows in original.items():
        original_ids = {row["id"] for row in rows}
        assert [row for row in after[table] if row["id"] in original_ids] == rows
    return after


def _review(brief_id, db_path):
    database.insert_reviewer_decision(
        ReviewerDecision(
            brief_id=brief_id,
            target_type="risk_domain",
            target_index=0,
            state="accepted",
            note="Reviewed before sample recovery.",
            decided_at=datetime(2026, 10, 25, 12, 0, tzinfo=timezone.utc),
        ),
        db_path,
    )


def _assert_all_samples_complete(db_path):
    for sample_path in BUNDLED_SAMPLES:
        sample = load_sample(sample_path)
        document_id = database.find_document_id(sample.source, sample.title, db_path)
        assert document_id is not None
        assert database.get_latest_brief_export(document_id, db_path) is not None
    with closing(database.connect(db_path)) as conn:
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def _insert_unlinked_run(conn, document_id, clean_text):
    cursor = conn.execute(
        """
        INSERT INTO processing_runs (
            document_id, processed_at, extractor_name, extractor_version,
            brief_schema_version, source_content_fingerprint
        ) VALUES (?, '2026-10-24T12:00:00+00:00', 'previous_extractor', '0.9.0', '1.0.0', ?)
        """,
        (document_id, source_content_fingerprint(clean_text)),
    )
    assert cursor.lastrowid is not None
    return cursor.lastrowid


def test_recovery_uses_the_requested_database_and_reports_only_new_results(tmp_path):
    db_path = tmp_path / "explicit.db"

    results = ensure_bundled_samples(db_path)

    assert len(results) == len(BUNDLED_SAMPLES)
    _assert_all_samples_complete(db_path)
    snapshot = _snapshot(db_path)
    assert {(row["document_id"], row["id"]) for row in snapshot["briefs"]} == set(results)
    assert ensure_bundled_samples(db_path) == []
    assert _snapshot(db_path) == snapshot


@pytest.mark.parametrize("initial_state", ["empty", "documents_only", "run_without_brief"])
def test_initialisation_recovers_missing_results_and_then_stops_writing(demo_db, initial_state):
    if initial_state != "empty":
        for sample_path in BUNDLED_SAMPLES:
            sample = load_sample(sample_path)
            document_id = database.insert_document(sample, demo_db)
            if initial_state == "run_without_brief":
                with closing(database.connect(demo_db)) as conn, conn:
                    _insert_unlinked_run(conn, document_id, sample.clean_text)

    original_document_ids = {row["id"] for row in database.get_documents(demo_db)}
    documents = streamlit_app._ensure_demo_data()

    assert len(documents) == len(BUNDLED_SAMPLES)
    assert original_document_ids.issubset({row["id"] for row in documents})
    _assert_all_samples_complete(demo_db)
    completed = _snapshot(demo_db)
    assert len(completed["briefs"]) == len(BUNDLED_SAMPLES)
    expected_runs = len(BUNDLED_SAMPLES) * (2 if initial_state == "run_without_brief" else 1)
    assert len(completed["processing_runs"]) == expected_runs
    assert streamlit_app._ensure_demo_data() == documents
    assert streamlit_app._ensure_demo_data() == documents
    assert _snapshot(demo_db) == completed


def test_unrelated_documents_do_not_hide_missing_samples(demo_db):
    sample = load_sample(BUNDLED_SAMPLES[0])
    identities = [
        ("unrelated_source", sample.title),
        (sample.source, "Unrelated title"),
        ("unrelated_source", "Unrelated title"),
    ]
    for source, title in identities:
        database.insert_document(sample.model_copy(update={"source": source, "title": title}), demo_db)
    original = _snapshot(demo_db)

    documents = streamlit_app._ensure_demo_data()

    assert len(documents) == len(identities) + len(BUNDLED_SAMPLES)
    _assert_all_samples_complete(demo_db)
    _assert_original_rows_retained(original, demo_db)


def test_recovery_preserves_reviewed_results_despite_source_and_extractor_changes(demo_db, tmp_path):
    sample = load_sample(BUNDLED_SAMPLES[0])
    earlier_sample = tmp_path / "earlier_sample.txt"
    earlier_sample.write_text(
        f"# SOURCE: {sample.source}\n"
        "# SOURCE_URL: https://example.org/earlier\n"
        f"TITLE: {sample.title}\n"
        "Fire safety works from an earlier source.\n",
        encoding="utf-8",
    )
    document_id, brief_id = run_pipeline(earlier_sample, demo_db)
    _review(brief_id, demo_db)
    with closing(database.connect(demo_db)) as conn, conn:
        conn.execute("UPDATE processing_runs SET extractor_version = '0.9.0'")
    original = _snapshot(demo_db)
    before = database.get_latest_brief_export(document_id, demo_db)
    assert before is not None
    original_json = serialize_brief_export(before).encode("utf-8")

    streamlit_app._ensure_demo_data()

    _assert_all_samples_complete(demo_db)
    after = _assert_original_rows_retained(original, demo_db)
    assert len(after["documents"]) == len(BUNDLED_SAMPLES)
    assert len(after["briefs"]) == len(BUNDLED_SAMPLES)
    assert len(after["processing_runs"]) == len(BUNDLED_SAMPLES)
    payload = database.get_latest_brief_export(document_id, demo_db)
    assert payload is not None
    assert serialize_brief_export(payload).encode("utf-8") == original_json
    assert payload.brief_id == brief_id
    assert len(payload.reviewer_decisions) == 1
    streamlit_app._ensure_demo_data()
    assert _snapshot(demo_db) == after


def test_brief_linked_to_another_documents_run_is_incomplete(demo_db):
    sample = load_sample(BUNDLED_SAMPLES[0])
    document_id = database.insert_document(sample, demo_db)
    other_id = database.insert_document(sample.model_copy(update={"source": "unrelated"}), demo_db)
    with closing(database.connect(demo_db)) as conn, conn:
        run_id = _insert_unlinked_run(conn, other_id, sample.clean_text)
        cursor = conn.execute(
            "INSERT INTO briefs (document_id, processing_run_id, summary) VALUES (?, ?, ?)",
            (document_id, run_id, "Brief with a mismatched document link."),
        )
        invalid_brief_id = cursor.lastrowid
    assert database.get_latest_brief_export(document_id, demo_db) is None
    original = _snapshot(demo_db)

    streamlit_app._ensure_demo_data()

    _assert_all_samples_complete(demo_db)
    _assert_original_rows_retained(original, demo_db)
    payload = database.get_latest_brief_export(document_id, demo_db)
    assert payload is not None
    assert payload.brief_id != invalid_brief_id
    assert payload.processing_run.id != run_id


def test_failed_recovery_keeps_completed_samples_and_retry_preserves_their_reviews(demo_db):
    with closing(database.connect(demo_db)) as conn, conn:
        conn.execute(
            "CREATE TRIGGER fail_second_sample BEFORE INSERT ON briefs "
            "WHEN (SELECT COUNT(*) FROM briefs) = 1 "
            "BEGIN SELECT RAISE(ABORT, 'forced sample failure'); END"
        )

    with pytest.raises(sqlite3.IntegrityError, match="forced sample failure"):
        streamlit_app._ensure_demo_data()

    partial = _snapshot(demo_db)
    assert len(partial["documents"]) == 1
    assert len(partial["processing_runs"]) == 1
    assert len(partial["briefs"]) == 1
    document_id = partial["documents"][0]["id"]
    brief_id = partial["briefs"][0]["id"]
    _review(brief_id, demo_db)
    original = _snapshot(demo_db)
    payload = database.get_latest_brief_export(document_id, demo_db)
    assert payload is not None
    original_json = serialize_brief_export(payload)
    with closing(database.connect(demo_db)) as conn, conn:
        conn.execute("DROP TRIGGER fail_second_sample")

    streamlit_app._ensure_demo_data()

    _assert_all_samples_complete(demo_db)
    after = _assert_original_rows_retained(original, demo_db)
    assert len(after["briefs"]) == len(BUNDLED_SAMPLES)
    payload = database.get_latest_brief_export(document_id, demo_db)
    assert payload is not None
    assert serialize_brief_export(payload) == original_json


def test_explicit_batch_ingestion_still_appends_new_results(demo_db):
    streamlit_app._ensure_demo_data()
    first_sample = load_sample(BUNDLED_SAMPLES[0])
    document_id = database.find_document_id(first_sample.source, first_sample.title, demo_db)
    assert document_id is not None
    original_payload = database.get_latest_brief_export(document_id, demo_db)
    assert original_payload is not None
    _review(original_payload.brief_id, demo_db)
    original = _snapshot(demo_db)

    results = ingest_bundled_samples(demo_db)

    assert len(results) == len(BUNDLED_SAMPLES)
    after = _assert_original_rows_retained(original, demo_db)
    assert len(after["documents"]) == len(BUNDLED_SAMPLES)
    assert len(after["processing_runs"]) == 2 * len(BUNDLED_SAMPLES)
    assert len(after["briefs"]) == 2 * len(BUNDLED_SAMPLES)
    payload = database.get_latest_brief_export(document_id, demo_db)
    assert payload is not None
    assert payload.brief_id != original_payload.brief_id
    assert payload.reviewer_decisions == []
    assert len(database.get_reviewer_decision_history(original_payload.brief_id, demo_db)) == 1


def test_initialisation_restores_legacy_provenance_without_reprocessing(tmp_path, monkeypatch):
    db_path = tmp_path / "legacy.db"
    original_connect = database.connect
    monkeypatch.setattr(
        database, "connect",
        lambda _path=database.DEFAULT_DB_PATH: original_connect(db_path),
    )
    sample = load_sample(BUNDLED_SAMPLES[0])
    with closing(database.connect(db_path)) as conn, conn:
        conn.executescript(
            """
            CREATE TABLE documents (
                id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT NOT NULL,
                source_url TEXT, title TEXT NOT NULL, raw_text TEXT NOT NULL,
                clean_text TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE TABLE briefs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                document_id INTEGER NOT NULL REFERENCES documents(id),
                summary TEXT NOT NULL, technical_scopes TEXT NOT NULL DEFAULT '[]',
                risk_domains TEXT NOT NULL DEFAULT '[]', missing_info TEXT NOT NULL DEFAULT '[]',
                review_questions TEXT NOT NULL DEFAULT '[]', evidence TEXT NOT NULL DEFAULT '[]',
                confidence TEXT NOT NULL DEFAULT 'low', human_review_required INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
            """
        )
        cursor = conn.execute(
            "INSERT INTO documents (source, source_url, title, raw_text, clean_text) "
            "VALUES (?, ?, ?, ?, ?)",
            (sample.source, "https://example.org/legacy", sample.title, "Earlier source", "Earlier source"),
        )
        assert cursor.lastrowid is not None
        document_id = cursor.lastrowid
        cursor = conn.execute(
            "INSERT INTO briefs (document_id, summary) VALUES (?, 'Legacy summary')",
            (document_id,),
        )
        assert cursor.lastrowid is not None
        brief_id = cursor.lastrowid
        original_document = dict(conn.execute("SELECT * FROM documents").fetchone())
        original_brief = dict(conn.execute("SELECT * FROM briefs").fetchone())

    streamlit_app._ensure_demo_data()

    _assert_all_samples_complete(db_path)
    payload = database.get_latest_brief_export(document_id, db_path)
    assert payload is not None
    assert payload.brief_id == brief_id
    assert payload.processing_run.extractor_name == LEGACY_EXTRACTOR_NAME
    assert payload.processing_run.source_content_fingerprint == source_content_fingerprint("Earlier source")
    snapshot = _snapshot(db_path)
    assert snapshot["documents"][0] == original_document
    assert {key: snapshot["briefs"][0][key] for key in original_brief} == original_brief
    assert len(snapshot["processing_runs"]) == len(BUNDLED_SAMPLES)
    assert len(snapshot["briefs"]) == len(BUNDLED_SAMPLES)
    streamlit_app._ensure_demo_data()
    assert _snapshot(db_path) == snapshot
