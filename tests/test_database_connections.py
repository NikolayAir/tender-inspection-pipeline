"""Read helpers must coexist with writers and respect caller-owned connections."""

from __future__ import annotations

from contextlib import closing

import pytest

from src.ai.risk_extract import extract_brief
from src.collect.sample_loader import load_sample
from src.db import database
from src.pipeline import BUNDLED_SAMPLES, run_pipeline
from src.provenance import build_processing_run


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
