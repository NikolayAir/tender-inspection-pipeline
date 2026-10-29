"""Source formatting remains visible without changing saved review data."""

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from src.db import database
from src.exports import serialize_brief_export
from src.models import ReviewerDecision
from src.pipeline import run_pipeline

APP_PATH = Path(__file__).resolve().parents[1] / "src" / "app" / "streamlit_app.py"
TITLE = "*Example* &amp; :red[notes]"
# A transparent synthetic image makes accidental Markdown rendering observable.
IMAGE = (
    "data:image/png;base64,"
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJ"
    "AAAADUlEQVR4nGNgYGBgAAAABQABpfZFQAAAAABJRU5ErkJggg=="
)
GAPS = [
    f"![not attached]({IMAGE}).",
    "The **schedule** is not included; :red[check] $x_1$ <draft> &amp; `plan`.",
]
SAVED_NOTE = "Keep **these markers**, :red[text] and <draft> literally."
DRAFT_NOTE = "Unsubmitted **draft** that must stay outside the JSON."


@pytest.fixture(params=GAPS, ids=["markdown-image", "formatting-and-entities"])
def literal_review(request, tmp_path, monkeypatch):
    gap = request.param
    raw_text = f"TITLE: {TITLE}\nA made-up example.\n{gap}"
    sample = tmp_path / "literal.txt"
    sample.write_text(f"# SOURCE: synthetic_sample\n{raw_text}\n", encoding="utf-8")
    db_path = tmp_path / "literal.db"
    connect = database.connect
    monkeypatch.setattr(database, "connect", lambda *_args, **_kwargs: connect(db_path))
    document_id, brief_id = run_pipeline(sample, db_path)
    database.insert_reviewer_decision(
        ReviewerDecision(
            brief_id=brief_id, target_type="missing_info", target_index=0,
            state="accepted", note=SAVED_NOTE,
            decided_at=datetime(2026, 10, 27, 12, tzinfo=timezone.utc),
        ), db_path,
    )
    app = AppTest.from_file(str(APP_PATH), default_timeout=20).run()
    label = next(option for option in app.selectbox(key="bundled_document").options if option.startswith(f"#{document_id} - "))
    app.selectbox(key="bundled_document").select(label).run()
    return app, db_path, document_id, brief_id, raw_text, gap


def _saved_json(document_id: int, db_path: Path) -> str:
    payload = database.get_latest_brief_export(document_id, db_path)
    assert payload is not None
    return serialize_brief_export(payload)


def test_saved_review_and_json_preview_preserve_literal_findings(literal_review):
    app, db_path, document_id, brief_id, raw_text, gap = literal_review
    before = _saved_json(document_id, db_path)
    exported = json.loads(before)
    assert exported["brief"]["missing_info"] == [gap]
    assert exported["brief"]["evidence"][0]["snippet"] == gap
    assert exported["document"]["title"] == TITLE
    assert exported["reviewer_decisions"][0]["target_index"] == 0
    assert any(item.value == f"• {gap}" for item in app.text)
    assert any(item.value == r"\*Example\* \&amp\; \:red\[notes\]" for item in app.subheader)
    assert any(item.value == raw_text for item in app.text)
    assert not any(gap in item.value for item in app.markdown)

    note_key = f"reviewer:{brief_id}:missing_info:0:note"
    app.text_area(key=note_key).set_value(DRAFT_NOTE)
    app.button(key="preview_export").click().run()

    assert not app.exception
    assert any(item.value == f"• {gap}" for item in app.text)
    assert any(item.value == f"Accepted · {gap}" for item in app.text)
    assert any(item.value == SAVED_NOTE for item in app.text)
    assert any(item.value == before.rstrip("\n") for item in app.code)
    assert not any(gap in item.value for item in app.markdown)
    assert app.text_area(key=note_key).value == DRAFT_NOTE
    assert _saved_json(document_id, db_path) == before
    snapshot = database.get_document_review(document_id, db_path)
    assert snapshot is not None
    assert snapshot[0]["raw_text"] == raw_text


def test_pasted_preview_displays_literal_findings_without_saving(literal_review):
    app, db_path, document_id, _, raw_text, gap = literal_review
    documents = database.get_documents(db_path)
    before = _saved_json(document_id, db_path)
    app.radio(key="workspace_nav").set_value("Analyse a public excerpt").run()
    app.text_area(key="adhoc_text").set_value(raw_text)
    next(button for button in app.button if button.label == "Generate review brief").click().run()

    assert not app.exception
    assert any(item.value == TITLE for item in app.text)
    assert any(item.value == f"• {gap}" for item in app.text)
    assert any(item.value == raw_text for item in app.text)
    assert not any(gap in item.value for item in app.markdown)
    document, brief = app.session_state["adhoc_preview"]
    assert document.raw_text == raw_text
    assert document.clean_text == raw_text
    assert brief.missing_info == [gap]
    assert database.get_documents(db_path) == documents
    assert _saved_json(document_id, db_path) == before
