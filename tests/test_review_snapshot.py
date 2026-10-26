"""Persisted review pages and exports must use a consistent SQLite snapshot."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from src import exports, pipeline
from src.collect.sample_loader import load_sample
from src.db import database
from src.models import ReviewerDecision
from src.provenance import source_content_fingerprint

APP_PATH = Path(__file__).resolve().parents[1] / "src" / "app" / "streamlit_app.py"


def _write_sample(path, *, updated=False):
    path.write_text(
        "# SOURCE: synthetic_sample\n"
        f"# SOURCE_URL: https://example.org/{'new' if updated else 'old'}\n"
        "TITLE: Review snapshot sample\n"
        + ("Electrical works.\n" if updated else "Fire safety works.\n"),
        encoding="utf-8",
    )


def _review(brief_id, note, db_path):
    database.insert_reviewer_decision(
        ReviewerDecision(
            brief_id=brief_id,
            target_type="risk_domain",
            target_index=0,
            state="accepted",
            note=note,
            decided_at=datetime(2026, 10, 26, 10, 0, tzinfo=timezone.utc),
        ),
        db_path,
    )


@pytest.fixture
def review_db(tmp_path, monkeypatch):
    db_path = tmp_path / "review.db"
    with closing(database.connect(db_path)) as conn:
        # WAL lets a second connection commit while a read snapshot remains open.
        assert conn.execute("PRAGMA journal_mode = WAL").fetchone()[0] == "wal"

    pending = []

    class InterleavedRead(sqlite3.Connection):
        def execute(self, sql, parameters=()):
            cursor = super().execute(sql, parameters)
            if pending and "ORDER BY pr.id DESC, b.id DESC" in sql:
                # SQLite has read the result; publish another version before it
                # is returned to the page or followed by the decision query.
                publish = pending.pop()
                publish()
            return cursor

    def connect(*_args, **_kwargs):
        conn = sqlite3.connect(db_path, timeout=1, factory=InterleavedRead)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    monkeypatch.setattr(database, "connect", connect)
    return db_path, pending


@pytest.mark.parametrize("publish_after", ["document_list", "brief_read"])
def test_page_keeps_source_findings_export_and_decisions_on_one_result(
    tmp_path, monkeypatch, review_db, publish_after,
):
    db_path, pending = review_db
    sample_path = tmp_path / "sample.txt"
    _write_sample(sample_path)
    document_id, old_brief_id = pipeline.run_pipeline(sample_path, db_path)
    _review(old_brief_id, "Saved Fire safety review.", db_path)
    original = database.get_latest_brief_export(document_id, db_path)
    assert original is not None
    original_json = exports.serialize_brief_export(original)
    monkeypatch.setattr(pipeline, "BUNDLED_SAMPLES", [sample_path])
    published = []

    def publish_new_result():
        _write_sample(sample_path, updated=True)
        _, brief_id = pipeline.run_pipeline(sample_path, db_path)
        _review(brief_id, "Saved Electrical review.", db_path)
        published.append(brief_id)

    if publish_after == "document_list":
        get_documents = database.get_documents

        def publish_after_listing(*args, **kwargs):
            documents = get_documents(*args, **kwargs)
            if not published:
                publish_new_result()
            return documents

        monkeypatch.setattr(database, "get_documents", publish_after_listing)
    else:
        pending.append(publish_new_result)

    downloads = []
    serialize = exports.serialize_brief_export

    def record_download(payload):
        data = serialize(payload)
        downloads.append(data)
        return data

    monkeypatch.setattr(exports, "serialize_brief_export", record_download)
    app = AppTest.from_file(str(APP_PATH), default_timeout=20).run()

    assert not app.exception
    assert not app.error
    assert len(published) == 1
    assert downloads
    payload = json.loads(downloads[-1])
    updated = publish_after == "document_list"
    expected_id = published[0] if updated else old_brief_id
    expected_domain = "Electrical" if updated else "Fire safety"
    expected_url = f"https://example.org/{'new' if updated else 'old'}"
    assert payload["brief_id"] == expected_id
    assert payload["brief"]["risk_domains"] == [expected_domain]
    assert payload["document"]["source_url"] == expected_url
    assert payload["reviewer_decisions"][0]["note"] == f"Saved {expected_domain} review."
    assert any(item.value == expected_domain for item in app.subheader)
    assert any(item.value == f"Saved {expected_domain} review." for item in app.text_area)
    source_text = next(
        item.value for item in app.text if "TITLE: Review snapshot sample" in item.value
    )
    assert expected_domain in source_text
    assert payload["processing_run"]["source_content_fingerprint"] == source_content_fingerprint(
        source_text
    )
    assert any(expected_url in item.value for item in app.markdown)
    if not updated:
        assert downloads[-1] == original_json

    key = f"reviewer:{expected_id}:risk_domain:0:state"
    reviewer = next(widget for widget in app.selectbox if widget.key == key)
    reviewer.select("rejected")
    note = next(widget for widget in app.text_area if widget.key == key.replace(":state", ":note"))
    note.set_value(f"Decision for displayed {expected_domain}.")
    next(button for button in app.button if button.label == "Save decision").click().run()

    assert not app.exception
    latest_history = database.get_reviewer_decision_history(published[0], db_path)
    if updated:
        assert latest_history[-1].note == "Decision for displayed Electrical."
        assert latest_history[-1].state == "rejected"
    else:
        # Submitting the older form must never attach its decision to the new
        # Electrical brief when Streamlit reruns the page.
        assert [event.note for event in latest_history] == ["Saved Electrical review."]


def test_export_keeps_decision_history_from_the_same_read_snapshot(tmp_path, review_db):
    db_path, pending = review_db
    sample_path = tmp_path / "sample.txt"
    _write_sample(sample_path)
    document_id, brief_id = pipeline.run_pipeline(sample_path, db_path)
    _review(brief_id, "Original review.", db_path)
    original = database.get_latest_brief_export(document_id, db_path)
    assert original is not None

    pending.append(lambda: _review(brief_id, "Decision saved during export.", db_path))
    snapshot = database.get_latest_brief_export(document_id, db_path)

    assert not pending
    assert snapshot is not None
    assert exports.serialize_brief_export(snapshot) == exports.serialize_brief_export(original)
    assert len(database.get_reviewer_decision_history(brief_id, db_path)) == 2


def test_document_without_a_brief_and_missing_document_have_no_review(tmp_path, review_db):
    db_path, _ = review_db
    database.init_db(db_path)
    sample_path = tmp_path / "sample.txt"
    _write_sample(sample_path)
    document_id = database.insert_document(load_sample(sample_path), db_path)

    snapshot = database.get_document_review(document_id, db_path)

    assert snapshot is not None
    document, payload = snapshot
    assert document["id"] == document_id
    assert document["clean_text"] == load_sample(sample_path).clean_text
    assert payload is None
    assert database.get_document_review(document_id + 1, db_path) is None
