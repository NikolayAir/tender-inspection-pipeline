"""Focused tests for persisted reviewer-decision UI helpers and submission."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

import pytest

import src.app.streamlit_app as streamlit_app
from src.app.streamlit_app import (
    review_state_label,
    reviewer_history_rows,
    reviewer_widget_key,
)
from src.models import StoredReviewerDecision


class _FakeForm:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False


class _ReviewerTargetStreamlit:
    def __init__(self, selected_state, note, *, submitted=True):
        self.selected_state = selected_state
        self.note = note
        self.submitted = submitted
        self.session_state = {}
        self.errors = []
        self.toasts = []
        self.warnings = []
        self.writes = []
        self.texts = []
        self.reruns = 0

    def markdown(self, *_args, **_kwargs):
        return None

    def write(self, *args, **_kwargs):
        self.writes.append(args)

    def text(self, value, **_kwargs):
        self.texts.append(value)

    def caption(self, *_args, **_kwargs):
        return None

    def toast(self, message, **kwargs):
        self.toasts.append((message, kwargs))

    def form(self, *_args, **_kwargs):
        return _FakeForm()

    def selectbox(self, *_args, **_kwargs):
        return self.selected_state

    def text_area(self, *_args, **_kwargs):
        return self.note

    def form_submit_button(self, *_args, **_kwargs):
        return self.submitted

    def warning(self, message):
        self.warnings.append(message)

    def error(self, message):
        self.errors.append(message)

    def rerun(self):
        self.reruns += 1


def test_reviewer_widget_key_contains_persisted_target_identity():
    assert (
        reviewer_widget_key(41, "missing_info", 2, "note")
        == "reviewer:41:missing_info:2:note"
    )


def test_review_state_labels_are_user_facing():
    assert review_state_label(None) == "Select a decision"
    assert review_state_label("accepted") == "Accepted"
    assert review_state_label("rejected") == "Rejected"
    assert review_state_label("needs_follow_up") == "Needs follow-up"


def test_reviewer_history_rows_filter_target_and_show_latest_first():
    history = [
        StoredReviewerDecision(
            id=1,
            brief_id=41,
            target_type="risk_domain",
            target_index=0,
            state="needs_follow_up",
            note="Check drawings.",
            decided_at=datetime(2026, 8, 10, 8, 0, tzinfo=timezone.utc),
        ),
        StoredReviewerDecision(
            id=2,
            brief_id=41,
            target_type="missing_info",
            target_index=0,
            state="rejected",
            note=None,
            decided_at=datetime(2026, 8, 10, 8, 30, tzinfo=timezone.utc),
        ),
        StoredReviewerDecision(
            id=3,
            brief_id=41,
            target_type="risk_domain",
            target_index=0,
            state="accepted",
            note="Drawings confirmed.",
            decided_at=datetime(2026, 8, 10, 9, 0, tzinfo=timezone.utc),
        ),
    ]

    rows = reviewer_history_rows(history, "risk_domain", 0)

    assert rows == [
        {
            "Decided at": "2026-08-10 09:00:00 UTC",
            "State": "Accepted",
            "Note": "Drawings confirmed.",
        },
        {
            "Decided at": "2026-08-10 08:00:00 UTC",
            "State": "Needs follow-up",
            "Note": "Check drawings.",
        },
    ]


def test_reviewer_target_rejects_incomplete_submission(monkeypatch):
    inserted = []
    fake_st = _ReviewerTargetStreamlit(
        selected_state=None,
        note="A note without a selected state.",
    )
    monkeypatch.setattr(streamlit_app, "st", fake_st)
    monkeypatch.setattr(
        streamlit_app.database,
        "insert_reviewer_decision",
        lambda decision: inserted.append(decision),
    )

    streamlit_app._render_reviewer_target(
        brief_id=41,
        target_type="risk_domain",
        target_index=0,
        generated_value="Fire safety",
        current=None,
        history=[],
    )

    assert inserted == []
    assert fake_st.warnings == ["Select a reviewer decision before saving."]
    assert fake_st.errors == []
    assert fake_st.reruns == 0


def test_reviewer_target_displays_current_state_and_note(monkeypatch):
    fake_st = _ReviewerTargetStreamlit(
        selected_state="accepted",
        note="Drawings confirmed.",
        submitted=False,
    )
    monkeypatch.setattr(streamlit_app, "st", fake_st)

    def unexpected_insert(_decision):
        raise AssertionError("Rendering must not append a decision")

    monkeypatch.setattr(
        streamlit_app.database,
        "insert_reviewer_decision",
        unexpected_insert,
    )

    current = StoredReviewerDecision(
        id=3,
        brief_id=41,
        target_type="risk_domain",
        target_index=0,
        state="accepted",
        note="Drawings confirmed.",
        decided_at=datetime(2026, 8, 10, 9, 0, tzinfo=timezone.utc),
    )

    streamlit_app._render_reviewer_target(
        brief_id=41,
        target_type="risk_domain",
        target_index=0,
        generated_value="Fire safety",
        current=current,
        history=[],
    )

    assert ("**Current review status:**", "Accepted") in fake_st.writes
    assert "Drawings confirmed." in fake_st.texts
    assert fake_st.reruns == 0


def test_reviewer_target_shows_saved_confirmation_as_toast(monkeypatch):
    fake_st = _ReviewerTargetStreamlit(
        selected_state="accepted",
        note="",
        submitted=False,
    )
    flash_key = "reviewer:41:risk_domain:0:saved"
    fake_st.session_state[flash_key] = "Reviewer decision saved."
    monkeypatch.setattr(streamlit_app, "st", fake_st)

    streamlit_app._render_reviewer_target(
        brief_id=41,
        target_type="risk_domain",
        target_index=0,
        generated_value="Fire safety",
        current=None,
        history=[],
    )

    assert fake_st.toasts == [("Reviewer decision saved.", {"icon": "✅"})]
    assert flash_key not in fake_st.session_state


@pytest.mark.parametrize(
    "error",
    [
        ValueError("invalid persisted target"),
        sqlite3.OperationalError("database is locked"),
    ],
)
def test_reviewer_target_reports_save_failure(monkeypatch, error):
    fake_st = _ReviewerTargetStreamlit(
        selected_state="accepted",
        note="Source evidence checked.",
    )
    monkeypatch.setattr(streamlit_app, "st", fake_st)

    def fail_to_insert(_decision):
        raise error

    monkeypatch.setattr(
        streamlit_app.database,
        "insert_reviewer_decision",
        fail_to_insert,
    )

    streamlit_app._render_reviewer_target(
        brief_id=41,
        target_type="risk_domain",
        target_index=0,
        generated_value="Fire safety",
        current=None,
        history=[],
    )

    assert len(fake_st.errors) == 1
    if isinstance(error, sqlite3.Error):
        assert "try saving again" in fake_st.errors[0]
        assert "Refresh" not in fake_st.errors[0]
    else:
        assert "Copy your note before refreshing" in fake_st.errors[0]
    assert fake_st.session_state == {}
    assert fake_st.reruns == 0


@pytest.mark.parametrize(
    "initial,identities,offset,expected",
    [
        (("risk_domain", 0), [("risk_domain", 0), ("risk_domain", 1)], -1, ("risk_domain", 0)),
        (("risk_domain", 1), [("risk_domain", 0), ("risk_domain", 1)], 1, ("risk_domain", 1)),
        (("risk_domain", 0), [("missing_info", 0)], 1, ("risk_domain", 0)),
        (None, [("risk_domain", 0)], 1, None),
    ],
)
def test_stale_navigation_events_do_not_wrap_or_crash(monkeypatch, initial, identities, offset, expected):
    fake_st = _ReviewerTargetStreamlit(None, "", submitted=False)
    if initial is not None:
        fake_st.session_state["queue"] = initial
    monkeypatch.setattr(streamlit_app, "st", fake_st)

    streamlit_app._move_finding("queue", identities, offset)

    assert fake_st.session_state.get("queue") == expected


def test_reviewer_target_saves_valid_append_only_event(monkeypatch):
    inserted = []
    fake_st = _ReviewerTargetStreamlit(
        selected_state="accepted",
        note="  Source evidence checked.  ",
    )
    monkeypatch.setattr(streamlit_app, "st", fake_st)
    monkeypatch.setattr(
        streamlit_app.database,
        "insert_reviewer_decision",
        lambda decision: inserted.append(decision),
    )

    streamlit_app._render_reviewer_target(
        brief_id=41,
        target_type="missing_info",
        target_index=2,
        generated_value="Drawings are not attached.",
        current=None,
        history=[],
    )

    assert len(inserted) == 1
    assert inserted[0].brief_id == 41
    assert inserted[0].target_type == "missing_info"
    assert inserted[0].target_index == 2
    assert inserted[0].state == "accepted"
    assert inserted[0].note == "Source evidence checked."
    assert inserted[0].decided_at.tzinfo == timezone.utc
    assert fake_st.session_state[
        "reviewer:41:missing_info:2:saved"
    ] == "Reviewer decision saved."
    assert fake_st.errors == []
    assert fake_st.reruns == 1
