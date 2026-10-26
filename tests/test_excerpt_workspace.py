"""Public previews remain session-only and survive ordinary UI interactions."""

from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from src.db import database

APP_PATH = Path(__file__).resolve().parents[1] / "src" / "app" / "streamlit_app.py"
EXCERPT = "TITLE: Public preview test\nFire safety works. Drawings are not attached."


@pytest.fixture
def excerpt_app(tmp_path, monkeypatch):
    db_path = tmp_path / "preview.db"
    connect = database.connect
    monkeypatch.setattr(database, "connect", lambda *_args, **_kwargs: connect(db_path))
    app = AppTest.from_file(str(APP_PATH), default_timeout=20).run()
    documents = database.get_documents(db_path)
    app.radio(key="workspace_nav").set_value("Analyse a public excerpt").run()
    return app, db_path, documents


def _generate(app, text=EXCERPT):
    app.text_area(key="adhoc_text").set_value(text)
    next(button for button in app.button if button.label == "Generate review brief").click().run()


def test_preview_never_renders_saved_decisions_or_writes_to_storage(excerpt_app):
    app, db_path, documents = excerpt_app
    assert not app.metric
    assert any("Your brief will appear here" == item.value for item in app.subheader)
    _generate(app)

    assert not app.exception
    assert app.metric[0].value == "1"
    assert app.metric[1].value == "1"
    assert [tab.label for tab in app.tabs] == ["Findings", "Evidence", "Questions", "Source"]
    assert not app.get("download_button")
    assert all(widget.label != "Reviewer decision" for widget in app.selectbox)
    assert database.get_documents(db_path) == documents
    with database.connect(db_path) as conn:
        assert conn.execute("SELECT count(*) FROM reviewer_decisions").fetchone()[0] == 0


def test_preview_and_submitted_input_survive_navigation_and_help(excerpt_app):
    app, _, _ = excerpt_app
    _generate(app)
    app.button(key="open_guide").click().run()
    assert not app.exception
    assert any(tab.label == "Quick start" for tab in app.tabs)
    assert app.metric[0].value == "1"
    app.radio(key="workspace_nav").set_value("Review workspace").run()
    app.radio(key="workspace_nav").set_value("Analyse a public excerpt").run()

    assert not app.exception
    assert app.text_area(key="adhoc_text").value == EXCERPT
    assert app.metric[0].value == "1"
    assert any(item.value == EXCERPT for item in app.text)


def test_invalid_resubmission_keeps_the_previous_preview_explicit(excerpt_app):
    app, _, _ = excerpt_app
    _generate(app)
    _generate(app, "   ")

    assert not app.exception
    assert any("Please paste" in message.value for message in app.warning)
    assert any("last generated preview" in message.value for message in app.info)
    assert app.metric[0].value == "1"
    assert any(item.value == EXCERPT for item in app.text)
    _generate(app, "Electrical installation works.")
    assert not app.exception
    assert not app.warning
    assert not app.info
    assert any(item.value == "Electrical installation works." for item in app.text)
