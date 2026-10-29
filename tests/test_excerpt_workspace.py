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
    app.button(key="generate_preview").click().run()


@pytest.mark.parametrize(
    ("field", "revised"),
    [("text", "Electrical installation works."), ("title", "Revised title"), ("url", "https://example.org/revised")],
)
def test_generation_action_returns_to_update_when_any_input_changes(excerpt_app, field, revised):
    app, db_path, documents = excerpt_app
    assert app.button(key="generate_preview").label == "Generate review brief"
    assert app.button(key="generate_preview").proto.type == "primary"
    _generate(app)
    assert not app.exception
    assert app.button(key="generate_preview").label == "Generate again"
    assert app.button(key="generate_preview").proto.type == "secondary"
    original = app.session_state["adhoc_preview"]
    widget = app.text_area(key="adhoc_text") if field == "text" else app.text_input(key=f"adhoc_{field}")
    previous = widget.value
    widget.set_value(revised).run()

    assert not app.exception
    assert app.button(key="generate_preview").label == "Update review brief"
    assert app.button(key="generate_preview").proto.type == "primary"
    assert any("Input has changed" in item.value for item in app.info)
    assert app.session_state["adhoc_preview"] == original
    assert database.get_documents(db_path) == documents

    widget = app.text_area(key="adhoc_text") if field == "text" else app.text_input(key=f"adhoc_{field}")
    widget.set_value(previous).run()
    assert app.button(key="generate_preview").label == "Generate again"
    assert app.button(key="generate_preview").proto.type == "secondary"
    assert app.session_state["adhoc_preview"] == original


def test_generating_again_remains_an_explicit_session_only_action(excerpt_app):
    app, db_path, documents = excerpt_app
    _generate(app)
    original = app.session_state["adhoc_preview"]
    app.button(key="generate_preview").click().run()

    assert not app.exception
    assert app.button(key="generate_preview").label == "Generate again"
    assert app.button(key="generate_preview").proto.type == "secondary"
    assert app.session_state["adhoc_preview"] == original
    assert database.get_documents(db_path) == documents


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
    assert all(button.key != "preview_export" for button in app.button)
    effects = [element for element in app.get("html") if element.proto.unsafe_allow_javascript]
    assert len(effects) == 1
    assert ".st-key-preview_result" in effects[0].proto.body
    app.run()
    assert not any(element.proto.unsafe_allow_javascript for element in app.get("html"))
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
    assert app.button(key="generate_preview").label == "Update review brief"
    assert app.button(key="generate_preview").proto.type == "primary"
    assert not any(element.proto.unsafe_allow_javascript for element in app.get("html"))
    assert app.metric[0].value == "1"
    assert any(item.value == EXCERPT for item in app.text)
    _generate(app, "Electrical installation works.")
    assert not app.exception
    assert not app.warning
    assert not app.info
    assert any(item.value == "Electrical installation works." for item in app.text)


def test_unsubmitted_draft_survives_help_and_navigation_without_analysis(excerpt_app, monkeypatch):
    from src.ai import risk_extract

    app, db_path, documents = excerpt_app

    def unexpected_analysis(_document):
        raise AssertionError("Editing a draft must not generate a brief")

    monkeypatch.setattr(risk_extract, "extract_brief", unexpected_analysis)
    app.text_area(key="adhoc_text").set_value(EXCERPT)
    app.text_input(key="adhoc_title").set_value("Unsubmitted draft title")
    app.text_input(key="adhoc_url").set_value("https://example.org/unsubmitted")
    app.button(key="open_guide").click().run()
    app.radio(key="workspace_nav").set_value("Review workspace").run()
    app.radio(key="workspace_nav").set_value("Analyse a public excerpt").run()

    assert not app.exception
    assert app.text_area(key="adhoc_text").value == EXCERPT
    assert app.text_input(key="adhoc_title").value == "Unsubmitted draft title"
    assert app.text_input(key="adhoc_url").value == "https://example.org/unsubmitted"
    assert "adhoc_preview" not in app.session_state
    assert not app.metric
    assert database.get_documents(db_path) == documents


def test_edited_draft_stays_distinct_from_last_preview_until_generation(excerpt_app):
    app, db_path, documents = excerpt_app
    _generate(app)
    revised = "TITLE: Revised excerpt\nHVAC and electrical installation works."
    app.text_area(key="adhoc_text").set_value(revised).run()

    assert not app.exception
    assert any("Input has changed" in message.value for message in app.info)
    assert app.metric[0].value == "1"
    assert any(item.value == EXCERPT for item in app.text)
    app.radio(key="workspace_nav").set_value("Review workspace").run()
    app.radio(key="workspace_nav").set_value("Analyse a public excerpt").run()
    assert app.text_area(key="adhoc_text").value == revised
    assert any(item.value == EXCERPT for item in app.text)
    assert any("Input has changed" in message.value for message in app.info)

    _generate(app, revised)
    assert not app.exception
    assert not app.info
    assert app.metric[0].value == "2"
    assert any(item.value == revised for item in app.text)
    assert database.get_documents(db_path) == documents


@pytest.mark.parametrize("optional_title", ["", "Title supplied by the reviewer"])
def test_preview_keeps_a_readable_title_when_the_excerpt_title_line_is_blank(excerpt_app, optional_title):
    app, db_path, documents = excerpt_app
    app.text_input(key="adhoc_title").set_value(optional_title)
    raw_text = "TITLE:   \nFire safety works."
    _generate(app, raw_text)

    assert not app.exception
    expected = optional_title or "Pasted public excerpt"
    assert any(item.value == expected for item in app.text)
    document, _brief = app.session_state["adhoc_preview"]
    assert document.title == expected
    assert document.raw_text == raw_text
    assert database.get_documents(db_path) == documents


def test_no_match_notice_is_not_counted_as_an_information_gap(excerpt_app):
    app, db_path, documents = excerpt_app
    _generate(app, "General public announcement.")

    assert not app.exception
    assert [metric.value for metric in app.metric] == ["0", "0", "0"]
    assert any("manual review needed" in item.value for item in app.info)
    assert any("No explicit phrases about missing information matched" in item.value for item in app.markdown)
    assert database.get_documents(db_path) == documents
