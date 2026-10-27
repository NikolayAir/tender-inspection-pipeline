"""The download viewer inspects saved data without applying review drafts."""

from datetime import datetime, timezone
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from src.ai.risk_extract import NO_MATCH_NOTICE
from src.db import database
from src.exports import serialize_brief_export
from src.models import ReviewerDecision
from src.pipeline import BUNDLED_SAMPLES, run_pipeline

APP_PATH = Path(__file__).resolve().parents[1] / "src" / "app" / "streamlit_app.py"


@pytest.fixture
def saved_review(tmp_path, monkeypatch):
    db_path = tmp_path / "download-preview.db"
    connect = database.connect
    monkeypatch.setattr(database, "connect", lambda *_args, **_kwargs: connect(db_path))
    document_id, brief_id = run_pipeline(BUNDLED_SAMPLES[0], db_path)
    for hour, state, note in (
        (9, "needs_follow_up", "Earlier saved note: check the fire drawings."),
        (10, "accepted", "Saved note: the fire drawings have been checked."),
    ):
        database.insert_reviewer_decision(
            ReviewerDecision(
                brief_id=brief_id, target_type="risk_domain", target_index=0,
                state=state, note=note,
                decided_at=datetime(2026, 10, 27, hour, tzinfo=timezone.utc),
            ), db_path,
        )
    return db_path, document_id, brief_id


def test_opening_download_preview_preserves_unsaved_form_and_saved_history(saved_review):
    db_path, document_id, brief_id = saved_review
    app = AppTest.from_file(str(APP_PATH), default_timeout=20).run()
    label = next(option for option in app.selectbox(key="bundled_document").options if option.startswith(f"#{document_id} - "))
    app.selectbox(key="bundled_document").select(label).run()
    before = serialize_brief_export(database.get_latest_brief_export(document_id, db_path))
    note_key = f"reviewer:{brief_id}:risk_domain:0:note"
    state_key = f"reviewer:{brief_id}:risk_domain:0:state"
    app.selectbox(key=state_key).select("rejected")
    app.text_area(key=note_key).set_value("Unsaved draft that must stay outside the download.")
    app.button(key="preview_export").click().run()

    assert not app.exception
    assert app.text_area(key=note_key).value == "Unsaved draft that must stay outside the download."
    assert app.selectbox(key=state_key).value == "rejected"
    assert serialize_brief_export(database.get_latest_brief_export(document_id, db_path)) == before
    # Streamlit's code display omits the canonical file's terminating newline.
    assert any(code.value == before.rstrip("\n") for code in app.code)
    assert any(text.value == "Saved note: the fire drawings have been checked." for text in app.text)
    assert any(text.value == "Earlier saved note: check the fire drawings." for text in app.text)
    assert not any("Unsaved draft" in text.value for text in app.text)
    assert any("Unsaved decisions and notes are excluded" in caption.value for caption in app.caption)
    assert all(download.proto.ignore_rerun for download in app.get("download_button"))
    assert not any(element.proto.unsafe_allow_javascript for element in app.get("html"))


def _standalone_preview(payload, json_text):
    from src.app.export_preview import show_export_preview

    show_export_preview(payload, json_text, "inspection-brief-document-1.json")


def test_viewer_and_exact_json_keep_the_supplied_snapshot_after_a_later_save(saved_review, monkeypatch):
    db_path, document_id, brief_id = saved_review
    captured = database.get_latest_brief_export(document_id, db_path)
    json_text = serialize_brief_export(captured)
    database.insert_reviewer_decision(
        ReviewerDecision(
            brief_id=brief_id, target_type="risk_domain", target_index=0,
            state="rejected", note="A later decision outside the captured snapshot.",
            decided_at=datetime(2026, 10, 27, 11, tzinfo=timezone.utc),
        ), db_path,
    )

    def unexpected_storage_access(*_args, **_kwargs):
        raise AssertionError("Viewing a captured download must not access storage")

    monkeypatch.setattr(database, "connect", unexpected_storage_access)
    app = AppTest.from_function(_standalone_preview, args=(captured, json_text)).run()

    assert not app.exception
    assert any(code.value == json_text.rstrip("\n") for code in app.code)
    assert any(text.value == "Saved note: the fire drawings have been checked." for text in app.text)
    assert not any("A later decision" in text.value for text in app.text)
    assert [tab.label for tab in app.tabs] == ["Brief", "Source evidence", "Saved decisions", "File details"]
    assert any(expander.label == "Complete decision history (2)" for expander in app.expander)
    assert any(text.value == captured.brief.evidence[0].snippet for text in app.text)
    assert serialize_brief_export(captured) == json_text


def test_unmatched_saved_brief_has_a_manual_notice_without_an_invented_gap_or_quote(tmp_path):
    sample = tmp_path / "unmatched.txt"
    sample.write_text("# SOURCE: synthetic_sample\nTITLE: Plain announcement\nGeneral public announcement.\n")
    db_path = tmp_path / "unmatched.db"
    document_id, _brief_id = run_pipeline(sample, db_path)
    payload = database.get_latest_brief_export(document_id, db_path)
    app = AppTest.from_function(
        _standalone_preview, args=(payload, serialize_brief_export(payload)),
    ).run()

    assert not app.exception
    assert any("Focus areas: 0 · Information gaps: 0 · Saved history entries: 0" == caption.value for caption in app.caption)
    assert any(message.value == NO_MATCH_NOTICE for message in app.info)
    assert any("No supporting source passages" in message.value for message in app.info)
    assert any(text.value == "Unreviewed" for text in app.markdown)
    assert any(expander.label == "Complete decision history (0)" for expander in app.expander)
