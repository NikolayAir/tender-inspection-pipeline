"""Interaction regressions for focused finding review and exact source links."""

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from src.ai.risk_extract import extract_brief
from src.app.review import Finding, evidence_for_finding
from src.collect.sample_loader import build_document_from_text, load_sample
from src.db import database
from src.models import ReviewerDecision
from src.pipeline import BUNDLED_SAMPLES, run_pipeline

APP_PATH = Path(__file__).resolve().parents[1] / "src" / "app" / "streamlit_app.py"


@pytest.fixture
def reviewed_app(tmp_path, monkeypatch):
    db_path = tmp_path / "workspace.db"
    connect = database.connect
    monkeypatch.setattr(database, "connect", lambda *_args, **_kwargs: connect(db_path))
    document_id, brief_id = run_pipeline(BUNDLED_SAMPLES[0], db_path)
    database.insert_reviewer_decision(
        ReviewerDecision(
            brief_id=brief_id, target_type="risk_domain", target_index=0,
            state="accepted", note="Initial review.", decided_at=datetime.now(timezone.utc),
        ), db_path,
    )
    app = AppTest.from_file(str(APP_PATH), default_timeout=20).run()
    label = next(option for option in app.selectbox(key="bundled_document").options if option.startswith(f"#{document_id} - "))
    app.selectbox(key="bundled_document").select(label).run()
    return app, db_path, brief_id


def test_filtered_queue_saves_to_original_target_and_updates_progress(reviewed_app):
    app, db_path, brief_id = reviewed_app
    app.selectbox(key=f"review_filter:{brief_id}").select("Unreviewed").run()

    assert not app.exception
    assert app.selectbox(key=f"review_queue:{brief_id}").value == ("risk_domain", 1)
    assert len(app.text_area) == 1
    app.selectbox(key=f"reviewer:{brief_id}:risk_domain:1:state").select("needs_follow_up")
    app.text_area(key=f"reviewer:{brief_id}:risk_domain:1:note").set_value("Check electrical drawings.")
    next(button for button in app.button if button.label == "Save decision").click().run()

    assert not app.exception
    history = database.get_reviewer_decision_history(brief_id, db_path)
    assert [(event.target_index, event.state) for event in history] == [(0, "accepted"), (1, "needs_follow_up")]
    assert history[-1].note == "Check electrical drawings."
    assert app.get("toast")
    assert not any(element.proto.unsafe_allow_javascript for element in app.get("html"))
    assert app.metric[3].value == "2 / 9"
    assert app.selectbox(key=f"review_queue:{brief_id}").value == ("risk_domain", 2)
    app.selectbox(key=f"review_filter:{brief_id}").select("Needs follow-up").run()
    assert app.selectbox(key=f"review_queue:{brief_id}").value == ("risk_domain", 1)
    assert app.text_area[0].value == "Check electrical drawings."
    assert not app.get("toast")


def test_next_previous_and_empty_filter_do_not_write_decisions(reviewed_app):
    app, db_path, brief_id = reviewed_app
    next(button for button in app.button if button.label == "Next").click().run()
    assert app.selectbox(key=f"review_queue:{brief_id}").value == ("risk_domain", 1)
    next(button for button in app.button if button.label == "Previous").click().run()
    assert app.selectbox(key=f"review_queue:{brief_id}").value == ("risk_domain", 0)
    app.selectbox(key=f"review_filter:{brief_id}").select("Rejected").run()

    assert not app.exception
    assert not app.text_area
    assert any("No findings match" in message.value for message in app.info)
    assert len(database.get_reviewer_decision_history(brief_id, db_path)) == 1


def test_failed_save_can_be_retried_without_losing_the_note(reviewed_app, monkeypatch):
    app, db_path, brief_id = reviewed_app
    insert = database.insert_reviewer_decision
    attempts = 0

    def fail_once(decision):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise sqlite3.OperationalError("database is locked")
        return insert(decision)

    monkeypatch.setattr(database, "insert_reviewer_decision", fail_once)
    app.selectbox(key=f"review_queue:{brief_id}").select(("risk_domain", 1)).run()
    app.selectbox(key=f"reviewer:{brief_id}:risk_domain:1:state").select("needs_follow_up")
    note = "Check the electrical drawings before accepting this finding."
    app.text_area(key=f"reviewer:{brief_id}:risk_domain:1:note").set_value(note)
    next(button for button in app.button if button.label == "Save decision").click().run()

    assert not app.exception
    assert any("try saving again" in error.value for error in app.error)
    assert app.text_area[0].value == note
    assert app.selectbox(key=f"reviewer:{brief_id}:risk_domain:1:state").value == "needs_follow_up"
    assert len(database.get_reviewer_decision_history(brief_id, db_path)) == 1
    assert not app.get("toast")

    next(button for button in app.button if button.label == "Save decision").click().run()

    assert not app.exception
    assert not app.error
    history = database.get_reviewer_decision_history(brief_id, db_path)
    assert len(history) == 2
    assert (history[-1].target_type, history[-1].target_index) == ("risk_domain", 1)
    assert history[-1].state == "needs_follow_up"
    assert history[-1].note == note
    assert app.get("toast")


def test_overview_action_opens_review_without_writing_a_decision(reviewed_app):
    app, db_path, brief_id = reviewed_app
    next(button for button in app.button if button.label == "Start reviewing").click().run()

    assert not app.exception
    assert app.session_state["brief_tabs"] == "Review findings"
    assert len(database.get_reviewer_decision_history(brief_id, db_path)) == 1
    effects = [element for element in app.get("html") if element.proto.unsafe_allow_javascript]
    assert len(effects) == 1
    assert ".st-key-brief_tabs" in effects[0].proto.body
    app.run()
    assert not any(element.proto.unsafe_allow_javascript for element in app.get("html"))


def test_gap_selection_saves_the_gap_index_and_shows_its_own_evidence(reviewed_app):
    app, db_path, brief_id = reviewed_app
    app.selectbox(key=f"review_queue:{brief_id}").select(("missing_info", 1)).run()
    assert not app.exception
    assert any("structural drawings" in item.value for item in app.text)
    app.selectbox(key=f"reviewer:{brief_id}:missing_info:1:state").select("rejected")
    app.text_area(key=f"reviewer:{brief_id}:missing_info:1:note").set_value("Checked separately.")
    next(button for button in app.button if button.label == "Save decision").click().run()

    assert not app.exception
    event = database.get_reviewer_decision_history(brief_id, db_path)[-1]
    assert (event.target_type, event.target_index, event.state) == ("missing_info", 1, "rejected")


def test_source_inspector_shows_full_selected_quote_and_preserves_source(reviewed_app):
    app, db_path, brief_id = reviewed_app
    document_id = next(document["id"] for document in database.get_documents(db_path) if document["source"] == "synthetic_sample")
    payload = database.get_latest_brief_export(document_id, db_path)
    assert payload is not None
    index = len(payload.brief.evidence) - 1
    app.selectbox(key=f"evidence_match:{document_id}").select(index).run()

    assert not app.exception
    assert any(item.value == payload.brief.evidence[index].snippet for item in app.text)
    assert any(item.value == load_sample().clean_text for item in app.text)
    assert len(database.get_reviewer_decision_history(brief_id, db_path)) == 1


def test_gap_evidence_is_linked_to_its_own_sentence_only():
    brief = extract_brief(load_sample())
    for index, value in enumerate(brief.missing_info):
        evidence = evidence_for_finding(brief, Finding("missing_info", index, value))
        assert evidence
        assert all(item.snippet == value for item in evidence)


def test_fallback_finding_does_not_borrow_another_findings_evidence():
    brief = extract_brief(load_sample())
    assert evidence_for_finding(brief, Finding("missing_info", 2, "Manual review needed.")) == []
    assert evidence_for_finding(brief, Finding("risk_domain", 0, "Unknown domain")) == []


def test_no_match_notice_keeps_its_review_identity_without_claiming_a_gap():
    brief = extract_brief(build_document_from_text("General public announcement."))
    finding = Finding("missing_info", 0, brief.missing_info[0])

    assert finding.kind == "Manual review notice"
    assert finding.identity == ("missing_info", 0)
    assert evidence_for_finding(brief, finding) == []
