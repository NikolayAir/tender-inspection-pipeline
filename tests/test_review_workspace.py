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
from src.exports import serialize_brief_export
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


def _workspace_payload(db_path):
    document_id = next(document["id"] for document in database.get_documents(db_path) if document["source"] == "synthetic_sample")
    payload = database.get_latest_brief_export(document_id, db_path)
    assert payload is not None
    return payload


def test_review_action_distinguishes_saved_values_from_unsaved_edits(reviewed_app):
    app, db_path, brief_id = reviewed_app
    save_key = f"reviewer:{brief_id}:risk_domain:0:save"
    state_key = f"reviewer:{brief_id}:risk_domain:0:state"
    note_key = f"reviewer:{brief_id}:risk_domain:0:note"
    original_json = serialize_brief_export(_workspace_payload(db_path))
    assert app.button(key=save_key).label == "Save again"
    assert app.button(key=save_key).proto.type == "secondary"
    assert any("Decision saved" in item.value for item in app.markdown)

    app.selectbox(key=state_key).select("rejected").run()
    app.text_area(key=note_key).set_value("Draft note awaiting a save.").run()
    assert not app.exception
    assert app.button(key=save_key).label == "Save changes"
    assert app.button(key=save_key).proto.type == "primary"
    assert any("Unsaved changes" in item.value for item in app.markdown)
    assert not any("Decision saved" in item.value for item in app.markdown)
    assert serialize_brief_export(_workspace_payload(db_path)) == original_json

    app.selectbox(key=state_key).select("accepted").run()
    app.text_area(key=note_key).set_value("Initial review.").run()
    assert app.button(key=save_key).label == "Save again"
    assert app.button(key=save_key).proto.type == "secondary"
    assert serialize_brief_export(_workspace_payload(db_path)) == original_json

    app.selectbox(key=state_key).select("rejected").run()
    app.text_area(key=note_key).set_value("Reviewed the complete source.").run()
    app.button(key=save_key).click().run()
    assert not app.exception
    assert app.button(key=save_key).label == "Save again"
    assert app.button(key=save_key).proto.type == "secondary"
    history = database.get_reviewer_decision_history(brief_id, db_path)
    assert len(history) == 2
    assert (history[-1].target_type, history[-1].target_index, history[-1].state) == ("risk_domain", 0, "rejected")
    assert history[-1].note == "Reviewed the complete source."
    app.button(key=save_key).click().run()
    assert len(database.get_reviewer_decision_history(brief_id, db_path)) == 3


def test_review_drafts_stay_with_their_finding_across_navigation_and_help(reviewed_app):
    app, db_path, brief_id = reviewed_app
    queue_key = f"review_queue:{brief_id}"
    first_note = f"reviewer:{brief_id}:risk_domain:0:note"
    second_note = f"reviewer:{brief_id}:risk_domain:1:note"
    original_json = serialize_brief_export(_workspace_payload(db_path))
    document_label = app.selectbox(key="bundled_document").value
    app.selectbox(key=f"reviewer:{brief_id}:risk_domain:0:state").select("rejected").run()
    app.text_area(key=first_note).set_value("Unsubmitted first-finding note.").run()
    app.selectbox(key=queue_key).select(("risk_domain", 1)).run()
    assert app.text_area(key=second_note).value == ""
    app.selectbox(key=f"reviewer:{brief_id}:risk_domain:1:state").select("needs_follow_up").run()
    app.text_area(key=second_note).set_value("Unsubmitted second-finding note.").run()
    app.selectbox(key=queue_key).select(("risk_domain", 0)).run()
    assert app.text_area(key=first_note).value == "Unsubmitted first-finding note."
    assert app.selectbox(key=f"reviewer:{brief_id}:risk_domain:0:state").value == "rejected"
    app.button(key="open_guide").click().run()
    app.radio(key="workspace_nav").set_value("Analyse a public excerpt").run()
    app.radio(key="workspace_nav").set_value("Review workspace").run()
    app.selectbox(key="bundled_document").select(document_label).run()
    assert not app.exception
    assert app.text_area(key=first_note).value == "Unsubmitted first-finding note."
    app.selectbox(key=queue_key).select(("risk_domain", 1)).run()
    assert app.text_area(key=second_note).value == "Unsubmitted second-finding note."
    assert app.selectbox(key=f"reviewer:{brief_id}:risk_domain:1:state").value == "needs_follow_up"
    assert serialize_brief_export(_workspace_payload(db_path)) == original_json


def test_failed_save_of_unchanged_fields_does_not_show_a_success_state(reviewed_app, monkeypatch):
    app, db_path, brief_id = reviewed_app
    save_key = f"reviewer:{brief_id}:risk_domain:0:save"
    insert = database.insert_reviewer_decision

    def fail(_decision):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(database, "insert_reviewer_decision", fail)
    app.button(key=save_key).click().run()
    assert not app.exception
    assert app.button(key=save_key).label == "Retry save"
    assert app.button(key=save_key).proto.type == "primary"
    assert not any("Decision saved" in item.value for item in app.markdown)
    assert any("not saved" in item.value for item in app.error)
    assert len(database.get_reviewer_decision_history(brief_id, db_path)) == 1

    monkeypatch.setattr(database, "insert_reviewer_decision", insert)
    app.button(key=save_key).click().run()
    assert not app.exception
    assert not app.error
    assert app.button(key=save_key).label == "Save again"
    assert app.button(key=save_key).proto.type == "secondary"
    assert len(database.get_reviewer_decision_history(brief_id, db_path)) == 2


def test_review_draft_is_not_transferred_to_a_reprocessed_brief(reviewed_app):
    app, db_path, old_brief_id = reviewed_app
    app.text_area(key=f"reviewer:{old_brief_id}:risk_domain:0:note").set_value("Draft for the older brief only.").run()
    _, new_brief_id = run_pipeline(BUNDLED_SAMPLES[0], db_path)
    app.run()

    assert not app.exception
    assert new_brief_id != old_brief_id
    assert app.text_area(key=f"reviewer:{new_brief_id}:risk_domain:0:note").value == ""
    assert app.button(key=f"reviewer:{new_brief_id}:risk_domain:0:save").label == "Save decision"
    assert database.get_reviewer_decision_history(new_brief_id, db_path) == []
    assert len(database.get_reviewer_decision_history(old_brief_id, db_path)) == 1
    assert app.session_state[f"reviewer:{old_brief_id}:risk_domain:0:draft"]["note"] == "Draft for the older brief only."


def test_successful_save_is_not_reported_as_failed_when_refreshing_the_review_fails(reviewed_app, monkeypatch):
    app, db_path, brief_id = reviewed_app
    query = database.get_document_review

    def unavailable_after_save(document_id):
        if len(database.get_reviewer_decision_history(brief_id, db_path)) > 1:
            raise sqlite3.OperationalError("could not reload the saved review")
        return query(document_id)

    monkeypatch.setattr(database, "get_document_review", unavailable_after_save)
    app.text_area(key=f"reviewer:{brief_id}:risk_domain:0:note").set_value("Saved even if the next read fails.").run()
    app.button(key=f"reviewer:{brief_id}:risk_domain:0:save").click().run()

    assert not app.exception
    assert any("Reviewer decision saved" in item.value for item in app.success)
    assert any("unavailable" in item.value for item in app.error)
    history = database.get_reviewer_decision_history(brief_id, db_path)
    assert len(history) == 2
    assert history[-1].note == "Saved even if the next read fails."
    assert "_pending_review_save" not in app.session_state
    monkeypatch.setattr(database, "get_document_review", query)
    # Follow the message's Refresh action by reopening the page.
    app = AppTest.from_file(str(APP_PATH), default_timeout=20).run()
    document_id = _workspace_payload(db_path).document.id
    label = next(option for option in app.selectbox(key="bundled_document").options if option.startswith(f"#{document_id} - "))
    app.selectbox(key="bundled_document").select(label).run()
    assert not app.exception
    assert not app.error
    assert len(database.get_reviewer_decision_history(brief_id, db_path)) == 2
    assert app.button(key=f"reviewer:{brief_id}:risk_domain:0:save").label == "Save again"


def test_filtered_queue_saves_to_original_target_and_updates_progress(reviewed_app):
    app, db_path, brief_id = reviewed_app
    app.selectbox(key=f"review_filter:{brief_id}").select("Unreviewed").run()

    assert not app.exception
    assert app.selectbox(key=f"review_queue:{brief_id}").value == ("risk_domain", 1)
    assert len(app.text_area) == 1
    app.selectbox(key=f"reviewer:{brief_id}:risk_domain:1:state").select("needs_follow_up")
    app.text_area(key=f"reviewer:{brief_id}:risk_domain:1:note").set_value("Check electrical drawings.")
    app.button(key=f"reviewer:{brief_id}:risk_domain:1:save").click().run()

    assert not app.exception
    history = database.get_reviewer_decision_history(brief_id, db_path)
    assert [(event.target_index, event.state) for event in history] == [(0, "accepted"), (1, "needs_follow_up")]
    assert history[-1].note == "Check electrical drawings."
    assert app.get("toast")
    assert not any(element.proto.unsafe_allow_javascript for element in app.get("html"))
    assert app.metric[3].value == "2 / 9"
    assert app.selectbox(key=f"review_queue:{brief_id}").value == ("risk_domain", 2)
    assert app.button(key=f"reviewer:{brief_id}:risk_domain:2:save").label == "Save decision"
    assert app.button(key=f"reviewer:{brief_id}:risk_domain:2:save").proto.type == "primary"
    assert not any("Decision saved" in item.value for item in app.markdown)
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
    app.button(key=f"reviewer:{brief_id}:risk_domain:1:save").click().run()

    assert not app.exception
    assert any("try saving again" in error.value for error in app.error)
    assert app.text_area[0].value == note
    assert app.selectbox(key=f"reviewer:{brief_id}:risk_domain:1:state").value == "needs_follow_up"
    assert len(database.get_reviewer_decision_history(brief_id, db_path)) == 1
    assert not app.get("toast")
    assert app.button(key=f"reviewer:{brief_id}:risk_domain:1:save").label == "Retry save"
    assert app.button(key=f"reviewer:{brief_id}:risk_domain:1:save").proto.type == "primary"

    app.button(key=f"reviewer:{brief_id}:risk_domain:1:save").click().run()

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


def test_run_details_keeps_the_original_summary_collapsed_and_unchanged(reviewed_app):
    app, db_path, _brief_id = reviewed_app
    document_id = next(document["id"] for document in database.get_documents(db_path) if document["source"] == "synthetic_sample")
    payload = database.get_latest_brief_export(document_id, db_path)
    assert payload is not None
    before = serialize_brief_export(payload)
    summaries = [section for section in app.expander if section.label == "Original generated summary"]

    assert len(summaries) == 1
    assert summaries[0].proto.expanded is False
    assert [item.value for item in summaries[0].text] == [payload.brief.summary]
    after = database.get_latest_brief_export(document_id, db_path)
    assert after is not None
    assert serialize_brief_export(after) == before


def test_gap_selection_saves_the_gap_index_and_shows_its_own_evidence(reviewed_app):
    app, db_path, brief_id = reviewed_app
    app.selectbox(key=f"review_queue:{brief_id}").select(("missing_info", 1)).run()
    assert not app.exception
    assert any("structural drawings" in item.value for item in app.text)
    app.selectbox(key=f"reviewer:{brief_id}:missing_info:1:state").select("rejected")
    app.text_area(key=f"reviewer:{brief_id}:missing_info:1:note").set_value("Checked separately.")
    app.button(key=f"reviewer:{brief_id}:missing_info:1:save").click().run()

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
