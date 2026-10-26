"""Interaction regressions for focused finding review and exact source links."""

from datetime import datetime, timezone
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from src.ai.risk_extract import extract_brief
from src.app.review import Finding, evidence_for_finding
from src.collect.sample_loader import load_sample
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
    assert not any(element.proto.unsafe_allow_javascript for element in app.get("html"))
    assert app.metric[3].value == "2 / 9"
    assert app.selectbox(key=f"review_queue:{brief_id}").value == ("risk_domain", 2)
    app.selectbox(key=f"review_filter:{brief_id}").select("Needs follow-up").run()
    assert app.selectbox(key=f"review_queue:{brief_id}").value == ("risk_domain", 1)
    assert app.text_area[0].value == "Check electrical drawings."


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
