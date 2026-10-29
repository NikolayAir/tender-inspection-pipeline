"""Storage helpers must close owned connections and preserve caller ownership."""

from __future__ import annotations

import sqlite3
from contextlib import closing, nullcontext
from datetime import datetime, timezone

import pytest

from src.ai.risk_extract import extract_brief
from src.collect.sample_loader import load_sample
from src.db import database
from src.models import ReviewerDecision
from src.pipeline import BUNDLED_SAMPLES, run_pipeline
from src.provenance import build_processing_run


@pytest.fixture
def tracked_database(tmp_path, monkeypatch):
    db_path = tmp_path / "owned-connections.db"
    document_id, brief_id = run_pipeline(BUNDLED_SAMPLES[0], db_path)
    original_connect = database.connect
    connections = []

    def tracked_connect(path=database.DEFAULT_DB_PATH):
        assert path == db_path
        conn = original_connect(path)
        # Keep references alive so garbage collection cannot hide a missing close.
        connections.append(conn)
        return conn

    monkeypatch.setattr(database, "connect", tracked_connect)
    decision = ReviewerDecision(
        brief_id=brief_id, target_type="risk_domain", target_index=0,
        state="accepted", note="Source evidence checked.",
        decided_at=datetime(2026, 10, 29, 12, tzinfo=timezone.utc),
    )
    try:
        yield db_path, document_id, decision, connections
    finally:
        for conn in connections:
            conn.close()


def _assert_connections_closed(connections):
    assert connections
    for conn in connections:
        with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
            conn.execute("SELECT 1")


@pytest.mark.parametrize("reader", ["documents", "brief", "missing_brief"])
def test_owned_read_connections_close_after_return(tracked_database, reader):
    db_path, document_id, _decision, connections = tracked_database

    if reader == "documents":
        assert [row["id"] for row in database.get_documents(db_path)] == [document_id]
    elif reader == "brief":
        brief = database.get_brief_for_document(document_id, db_path)
        assert brief is not None
        assert "Fire safety" in brief.risk_domains
    else:
        assert database.get_brief_for_document(-1, db_path) is None

    assert len(connections) == 1
    _assert_connections_closed(connections)


@pytest.mark.parametrize("reader", ["documents", "brief"])
def test_owned_read_connections_close_when_query_fails(tracked_database, reader):
    db_path, document_id, _decision, connections = tracked_database
    table = "documents" if reader == "documents" else "briefs"
    with closing(sqlite3.connect(db_path)) as conn, conn:
        conn.execute(f"DROP TABLE {table}")

    with pytest.raises(sqlite3.OperationalError, match=f"no such table: {table}"):
        if reader == "documents":
            database.get_documents(db_path)
        else:
            database.get_brief_for_document(document_id, db_path)

    _assert_connections_closed(connections)


def test_reviewer_save_commits_and_closes_its_connection(tracked_database):
    db_path, _document_id, decision, connections = tracked_database
    decision_id = database.insert_reviewer_decision(decision, db_path)

    _assert_connections_closed(connections)
    with closing(sqlite3.connect(db_path)) as conn:
        rows = conn.execute(
            "SELECT id, brief_id, target_type, target_index, state, note "
            "FROM reviewer_decisions"
        ).fetchall()
    assert rows == [
        (
            decision_id, decision.brief_id, "risk_domain", 0,
            "accepted", "Source evidence checked.",
        )
    ]


@pytest.mark.parametrize("invalid_field", ["brief_id", "target_index"])
def test_reviewer_save_closes_its_connection_on_invalid_target(
    tracked_database, invalid_field,
):
    db_path, _document_id, decision, connections = tracked_database
    invalid = decision.model_copy(update={invalid_field: 999})

    with pytest.raises(ValueError, match=invalid_field):
        database.insert_reviewer_decision(invalid, db_path)

    _assert_connections_closed(connections)
    with closing(sqlite3.connect(db_path)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM reviewer_decisions").fetchone()[0] == 0


def test_reviewer_save_rolls_back_and_closes_on_error_after_insert(
    tracked_database, monkeypatch,
):
    db_path, _document_id, decision, connections = tracked_database
    earlier_id = database.insert_reviewer_decision(decision, db_path)

    def fail_after_insert(_cursor):
        raise RuntimeError("failure after decision insert")

    monkeypatch.setattr(database, "_required_lastrowid", fail_after_insert)
    later = decision.model_copy(update={"state": "rejected", "note": "Failed update."})
    with pytest.raises(RuntimeError, match="failure after decision insert"):
        database.insert_reviewer_decision(later, db_path)

    _assert_connections_closed(connections)
    with closing(sqlite3.connect(db_path)) as conn:
        rows = conn.execute("SELECT id, state, note FROM reviewer_decisions").fetchall()
    assert rows == [(earlier_id, "accepted", "Source evidence checked.")]


@pytest.mark.parametrize("raise_error", [False, True], ids=["success", "exception"])
def test_borrowed_transaction_leaves_completion_and_close_to_caller(
    tracked_database, raise_error,
):
    db_path, _document_id, _decision, _connections = tracked_database
    sample = load_sample(BUNDLED_SAMPLES[0]).model_copy(
        update={"title": "Caller-owned transaction"},
    )
    expected = (
        pytest.raises(RuntimeError, match="caller-owned error")
        if raise_error else nullcontext()
    )

    with closing(database.connect(db_path)) as caller:
        caller.execute("BEGIN")
        with expected, database.transaction(connection=caller) as borrowed:
            assert borrowed is caller
            document_id = database.insert_document(sample, connection=borrowed)
            if raise_error:
                raise RuntimeError("caller-owned error")

        assert caller.in_transaction
        assert caller.execute(
            "SELECT title FROM documents WHERE id = ?", (document_id,),
        ).fetchone()["title"] == "Caller-owned transaction"
        assert database.find_document_id(sample.source, sample.title, db_path) is None
        caller.rollback()
        assert caller.execute(
            "SELECT COUNT(*) FROM documents WHERE id = ?", (document_id,),
        ).fetchone()[0] == 0


@pytest.mark.parametrize("existing", [False, True], ids=["missing", "existing"])
def test_document_lookup_can_read_while_another_connection_reserves_the_writer(
    tmp_path, monkeypatch, existing,
):
    db_path = tmp_path / "readers.db"
    document_id, _ = run_pipeline(BUNDLED_SAMPLES[0], db_path)
    sample = load_sample(BUNDLED_SAMPLES[0])
    title = sample.title if existing else "Missing tender"
    original_connect = database.connect

    def short_timeout(*args, **kwargs):
        conn = original_connect(*args, **kwargs)
        # Fail promptly if a read helper accidentally tries to reserve the SQLite writer.
        conn.execute("PRAGMA busy_timeout = 100")
        return conn

    monkeypatch.setattr(database, "connect", short_timeout)
    with closing(original_connect(db_path)) as writer:
        writer.execute("BEGIN IMMEDIATE")
        try:
            assert database.find_document_id(sample.source, title, db_path) == (
                document_id if existing else None
            )
            assert database.has_completed_result(sample.source, title, db_path) is existing
            assert [row["id"] for row in database.get_documents(db_path)] == [document_id]
            brief = database.get_brief_for_document(document_id if existing else -1, db_path)
            assert (brief is not None) is existing
            review = database.get_document_review(document_id if existing else -1, db_path)
            if existing:
                assert review is not None
                document, payload = review
                assert document["id"] == document_id
                assert payload is not None
                assert payload.document.id == document_id
            else:
                assert review is None
        finally:
            writer.rollback()


def test_read_helpers_see_uncommitted_results_on_the_supplied_connection(tmp_path):
    db_path = tmp_path / "borrowed.db"
    database.init_db(db_path)
    sample = load_sample(BUNDLED_SAMPLES[0])

    with pytest.raises(RuntimeError, match="caller rollback"):
        with database.transaction(db_path) as conn:
            document_id = database.insert_document(sample, connection=conn)
            database.insert_processing_result(
                build_processing_run(document_id, sample),
                extract_brief(sample),
                connection=conn,
            )

            assert database.find_document_id(
                sample.source, sample.title, connection=conn,
            ) == document_id
            assert database.has_completed_result(
                sample.source, sample.title, connection=conn,
            )
            assert conn.in_transaction
            assert database.find_document_id(sample.source, sample.title, db_path) is None
            assert not database.has_completed_result(sample.source, sample.title, db_path)
            raise RuntimeError("caller rollback")

    assert database.get_documents(db_path) == []
    assert not database.has_completed_result(sample.source, sample.title, db_path)
