"""Saved reviews remain usable when another bundled sample cannot be recovered."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from src.collect.sample_loader import load_sample
from src.db import database
from src.exports import serialize_brief_export
from src.models import ReviewerDecision
from src.pipeline import BUNDLED_SAMPLES, run_pipeline

APP_PATH = Path(__file__).resolve().parents[1] / "src" / "app" / "streamlit_app.py"
REVIEW_NOTE = "Reviewed before automatic sample recovery."


@pytest.fixture
def app_db(tmp_path, monkeypatch):
    db_path = tmp_path / "app.db"
    original_connect = database.connect
    monkeypatch.setattr(database, "connect", lambda *_args, **_kwargs: original_connect(db_path))
    database.init_db(db_path)
    return db_path


def _reviewed_sample(db_path):
    document_id, brief_id = run_pipeline(BUNDLED_SAMPLES[0], db_path)
    database.insert_reviewer_decision(
        ReviewerDecision(
            brief_id=brief_id,
            target_type="risk_domain",
            target_index=0,
            state="accepted",
            note=REVIEW_NOTE,
            decided_at=datetime(2026, 10, 25, 12, 0, tzinfo=timezone.utc),
        ),
        db_path,
    )
    payload = database.get_latest_brief_export(document_id, db_path)
    assert payload is not None
    return document_id, serialize_brief_export(payload)


def _fail_recovery(db_path):
    with closing(database.connect(db_path)) as conn, conn:
        conn.execute(
            "CREATE TRIGGER fail_recovery BEFORE INSERT ON briefs "
            "BEGIN SELECT RAISE(ABORT, 'forced recovery failure'); END"
        )


def _recovery_warnings(app):
    return [item.value for item in app.warning if "saved documents" in item.value.lower()]


@pytest.mark.parametrize("unrelated_count", [0, 2], ids=["partial_demo", "enough_other_documents"])
def test_failed_recovery_keeps_saved_reviews_and_download_available_then_retries(
    app_db, unrelated_count,
):
    document_id, original_json = _reviewed_sample(app_db)
    sample = load_sample(BUNDLED_SAMPLES[0])
    for index in range(unrelated_count):
        database.insert_document(
            sample.model_copy(update={"source": "unrelated", "title": f"Unrelated {index}"}),
            app_db,
        )
    original_documents = database.get_documents(app_db)
    _fail_recovery(app_db)

    app = AppTest.from_file(str(APP_PATH), default_timeout=20).run()

    assert not app.exception
    assert not app.error
    assert len(_recovery_warnings(app)) == 1
    selector = next(widget for widget in app.selectbox if widget.label == "Select a document")
    selected_label = next(option for option in selector.options if option.startswith(f"#{document_id} - "))
    selector.select(selected_label).run()
    assert not app.exception
    assert any(widget.value == REVIEW_NOTE for widget in app.text_area)
    assert app.get("download_button")
    app.run()
    assert not app.exception
    assert any(widget.value == REVIEW_NOTE for widget in app.text_area)
    assert database.get_documents(app_db) == original_documents
    payload = database.get_latest_brief_export(document_id, app_db)
    assert payload is not None
    assert serialize_brief_export(payload) == original_json

    with closing(database.connect(app_db)) as conn, conn:
        conn.execute("DROP TRIGGER fail_recovery")
    app.run()

    assert not app.exception
    assert not app.error
    assert not _recovery_warnings(app)
    assert any(widget.value == REVIEW_NOTE for widget in app.text_area)
    assert app.get("download_button")
    assert len(database.get_documents(app_db)) == unrelated_count + len(BUNDLED_SAMPLES)
    payload = database.get_latest_brief_export(document_id, app_db)
    assert payload is not None
    assert serialize_brief_export(payload) == original_json


def test_failed_initial_recovery_shows_an_empty_database_error(app_db):
    _fail_recovery(app_db)

    app = AppTest.from_file(str(APP_PATH), default_timeout=20).run()

    assert not app.exception
    assert not _recovery_warnings(app)
    assert len(app.error) == 1
    assert "No documents found" in app.error[0].value
    assert all(widget.label != "Select a document" for widget in app.selectbox)
    assert database.get_documents(app_db) == []


def test_unreadable_database_shows_unavailability_and_logs_the_cause(app_db, monkeypatch, caplog):
    def unavailable(*_args, **_kwargs):
        raise sqlite3.OperationalError("simulated unavailable database")

    monkeypatch.setattr(database, "connect", unavailable)

    app = AppTest.from_file(str(APP_PATH), default_timeout=20).run()

    assert not app.exception
    assert len(app.error) == 1
    assert "unavailable" in app.error[0].value.lower()
    assert all(widget.label != "Select a document" for widget in app.selectbox)
    assert any(record.exc_info for record in caplog.records)


def test_saved_review_query_failure_is_reported_after_recovery_fallback(app_db, monkeypatch):
    _reviewed_sample(app_db)
    _fail_recovery(app_db)

    def unavailable(*_args, **_kwargs):
        raise sqlite3.OperationalError("simulated unavailable review")

    monkeypatch.setattr(database, "get_brief_for_document", unavailable)

    app = AppTest.from_file(str(APP_PATH), default_timeout=20).run()

    assert not app.exception
    assert len(_recovery_warnings(app)) == 1
    assert len(app.error) == 1
    assert "saved review" in app.error[0].value.lower()
    assert "unavailable" in app.error[0].value.lower()
    assert not app.get("download_button")
