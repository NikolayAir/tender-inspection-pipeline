"""Processing failures must leave persisted sources and reviewed results intact."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import datetime, timezone

import pytest

from src import pipeline
from src.collect.sample_loader import load_sample
from src.db import database
from src.exports import VersionedBriefExportV1_0, serialize_brief_export
from src.models import ReviewerDecision
from src.provenance import source_content_fingerprint


def _write_sample(path, *, updated=False):
    url = "https://example.org/new" if updated else "https://example.org/old"
    works = "Electrical works." if updated else "Fire safety works."
    path.write_text(
        "# SOURCE: transaction_test\n"
        f"# SOURCE_URL: {url}\n"
        "TITLE: Same tender\n"
        f"{works}\n",
        encoding="utf-8",
    )


def _snapshot(db_path):
    with closing(database.connect(db_path)) as conn:
        return {
            table: [dict(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY id")]
            for table in ("documents", "processing_runs", "briefs", "reviewer_decisions")
        }


def _review(brief_id, db_path):
    for state in ("needs_follow_up", "accepted"):
        database.insert_reviewer_decision(
            ReviewerDecision(
                brief_id=brief_id,
                target_type="risk_domain",
                target_index=0,
                state=state,
                note="Check fire-safety drawings.",
                decided_at=datetime(2026, 10, 25, 12, 0, tzinfo=timezone.utc),
            ),
            db_path,
        )


def _export_bytes(document_id, db_path):
    payload = database.get_latest_brief_export(document_id, db_path)
    assert payload is not None
    legacy = VersionedBriefExportV1_0(
        document=payload.document,
        processing_run=payload.processing_run,
        brief_id=payload.brief_id,
        brief=payload.brief,
    )
    return tuple(serialize_brief_export(item).encode("utf-8") for item in (payload, legacy))


@pytest.mark.parametrize("reprocessing", [False, True], ids=["initial", "reviewed"])
@pytest.mark.parametrize(
    "failure_stage",
    ["extraction", "run_preparation", "documents", "processing_runs", "briefs", "commit"],
)
def test_failed_processing_preserves_all_stored_data_and_exports(
    tmp_path, monkeypatch, reprocessing, failure_stage,
):
    db_path = tmp_path / "transaction.db"
    sample_path = tmp_path / "sample.txt"
    database.init_db(db_path)
    _write_sample(sample_path)

    previous_result = None
    if reprocessing:
        document_id, brief_id = pipeline.run_pipeline(sample_path, db_path)
        _review(brief_id, db_path)
        previous_result = document_id, brief_id, _export_bytes(document_id, db_path)

    original = _snapshot(db_path)
    _write_sample(sample_path, updated=True)
    error_message = "forced processing failure"

    if failure_stage in {"extraction", "run_preparation"}:
        def fail(*_args, **_kwargs):
            raise RuntimeError("forced processing failure")

        monkeypatch.setattr(
            pipeline,
            "extract_brief" if failure_stage == "extraction" else "build_processing_run",
            fail,
        )
        error_type = RuntimeError
    elif failure_stage == "commit":
        # Defer a deliberately invalid foreign key so inserts succeed and COMMIT
        # itself fails, exercising rollback after all source and result writes.
        with closing(database.connect(db_path)) as conn, conn:
            conn.execute(
                "CREATE TRIGGER fail_on_commit AFTER INSERT ON briefs BEGIN "
                "INSERT INTO reviewer_decisions "
                "(brief_id, target_type, target_index, state, decided_at) "
                "VALUES (-1, 'risk_domain', 0, 'accepted', '2026-10-25T12:00:00+00:00'); END"
            )

        original_connect = database.connect

        def defer_foreign_keys(*args, **kwargs):
            connection = original_connect(*args, **kwargs)
            connection.execute("PRAGMA defer_foreign_keys = ON")
            return connection

        monkeypatch.setattr(database, "connect", defer_foreign_keys)
        error_type = sqlite3.IntegrityError
        error_message = "FOREIGN KEY constraint failed"
    else:
        operation = "UPDATE" if reprocessing and failure_stage == "documents" else "INSERT"
        with closing(database.connect(db_path)) as conn, conn:
            conn.execute(
                f"CREATE TRIGGER fail_attempt AFTER {operation} ON {failure_stage} "
                "BEGIN SELECT RAISE(ABORT, 'forced processing failure'); END"
            )
        error_type = sqlite3.IntegrityError

    with pytest.raises(error_type, match=error_message):
        pipeline.run_pipeline(sample_path, db_path)

    assert _snapshot(db_path) == original
    if previous_result is not None:
        document_id, brief_id, original_exports = previous_result
        assert _export_bytes(document_id, db_path) == original_exports
        document = database.get_documents(db_path)[0]
        payload = database.get_latest_brief_export(document_id, db_path)
        assert payload is not None
        assert payload.brief_id == brief_id
        assert payload.document.source_url == "https://example.org/old"
        assert payload.processing_run.source_content_fingerprint == source_content_fingerprint(
            document["clean_text"]
        )

    with closing(database.connect(db_path)) as conn:
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_explicit_reprocessing_updates_source_and_keeps_decisions_on_original_brief(tmp_path):
    db_path = tmp_path / "reprocessing.db"
    sample_path = tmp_path / "sample.txt"
    _write_sample(sample_path)
    document_id, original_brief_id = pipeline.run_pipeline(sample_path, db_path)
    _review(original_brief_id, db_path)
    original = _snapshot(db_path)

    _write_sample(sample_path, updated=True)
    updated_document_id, new_brief_id = pipeline.run_pipeline(sample_path, db_path)

    assert updated_document_id == document_id
    assert new_brief_id != original_brief_id
    after = _snapshot(db_path)
    assert len(after["documents"]) == 1
    document = after["documents"][0]
    expected = load_sample(sample_path)
    assert document["source_url"] == expected.source_url
    assert document["raw_text"] == expected.raw_text
    assert document["clean_text"] == expected.clean_text
    assert document["created_at"] == original["documents"][0]["created_at"]
    assert after["processing_runs"][:-1] == original["processing_runs"]
    assert len(after["processing_runs"]) == len(original["processing_runs"]) + 1
    assert after["briefs"][:-1] == original["briefs"]
    assert len(after["briefs"]) == len(original["briefs"]) + 1
    assert after["reviewer_decisions"] == original["reviewer_decisions"]

    payload = database.get_latest_brief_export(document_id, db_path)
    assert payload is not None
    assert payload.brief_id == new_brief_id
    assert payload.document.source_url == "https://example.org/new"
    assert after["processing_runs"][-1]["document_id"] == document_id
    assert after["briefs"][-1]["document_id"] == document_id
    assert after["briefs"][-1]["processing_run_id"] == payload.processing_run.id
    assert payload.processing_run.source_content_fingerprint == source_content_fingerprint(
        expected.clean_text
    )
    assert payload.reviewer_decisions == []
    assert len(database.get_reviewer_decision_history(original_brief_id, db_path)) == 2
    with closing(database.connect(db_path)) as conn:
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
