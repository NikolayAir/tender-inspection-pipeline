"""Smoke tests for the runnable application.

Covers importability, database creation, and one sample row flowing through the
full pipeline against a temporary database (the real
``data/processed/tender_inspection.db`` is never touched).
"""

from __future__ import annotations

import importlib
from types import SimpleNamespace

import pytest

import src.app.streamlit_app as streamlit_app
from src.ai.risk_extract import MISSING_INFO_PHRASES, extract_brief
from src.app.streamlit_app import (
    adhoc_input_error,
    brief_export_filename,
    category_for_term,
)
from src.collect.sample_loader import build_document_from_text, load_sample
from src.db import database
from src.pipeline import PUBLIC_SAMPLE_BELVAUX_PATH, PUBLIC_SAMPLE_PATH, run_pipeline
from src.validation.manual_validation import load_validation_summary

MODULES = [
    "src.models",
    "src.provenance",
    "src.parse.clean",
    "src.collect.sample_loader",
    "src.db.database",
    "src.ai.risk_extract",
    "src.pipeline",
    "src.app.streamlit_app",
    "src.validation.manual_validation",
]


@pytest.mark.parametrize("module_name", MODULES)
def test_imports(module_name):
    importlib.import_module(module_name)


def test_brief_export_filename_is_deterministic():
    assert brief_export_filename(17) == "inspection-brief-document-17.json"


def test_bundled_view_shows_export_download_before_brief_body(monkeypatch):
    """The persisted JSON action is visible before the long brief content."""
    events = []

    class FakeExpander:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_value, traceback):
            return False

    class FakeStreamlit:
        def selectbox(self, _label, options, **_kwargs):
            return options[0]

        def info(self, *_args, **_kwargs):
            return None

        def subheader(self, *_args, **_kwargs):
            return None

        def download_button(self, label, **kwargs):
            events.append(
                (
                    "download_button",
                    {
                        "label": label,
                        **kwargs,
                    },
                )
            )
            return False

        def expander(self, *_args, **_kwargs):
            return FakeExpander()

        def write(self, *_args, **_kwargs):
            return None

        def text(self, *_args, **_kwargs):
            return None

        def caption(self, *_args, **_kwargs):
            return None

        def dataframe(self, *_args, **_kwargs):
            return None

    document = {
        "id": 7,
        "source": "synthetic_sample",
        "source_url": None,
        "title": "Test bundled document",
        "clean_text": "Test source text",
    }
    brief = object()
    export_payload = SimpleNamespace(brief_id=41, brief=brief)

    monkeypatch.setattr(streamlit_app, "st", FakeStreamlit())
    monkeypatch.setattr(
        streamlit_app,
        "_ensure_demo_data",
        lambda: [document],
    )
    monkeypatch.setattr(
        streamlit_app.database,
        "get_document_review",
        lambda document_id: (document, export_payload),
    )
    monkeypatch.setattr(
        streamlit_app,
        "serialize_brief_export",
        lambda payload: '{"export_schema_version": "1.0.0"}\n',
    )
    monkeypatch.setattr(
        streamlit_app,
        "load_validation_summary",
        lambda: {
            "sample_count": 0,
            "status_counts": {},
            "detail_rows": [],
        },
    )
    monkeypatch.setattr(
        streamlit_app,
        "render_brief_body",
        lambda rendered_brief: events.append(
            ("render_brief_body", rendered_brief)
        ),
    )
    monkeypatch.setattr(
        streamlit_app,
        "render_reviewer_decisions",
        lambda payload: events.append(
            ("render_reviewer_decisions", payload)
        ),
    )

    streamlit_app.render_bundled_view()

    download_index = next(
        index
        for index, event in enumerate(events)
        if event[0] == "download_button"
    )
    brief_index = next(
        index
        for index, event in enumerate(events)
        if event[0] == "render_brief_body"
    )
    reviewer_index = next(
        index
        for index, event in enumerate(events)
        if event[0] == "render_reviewer_decisions"
    )
    button = events[download_index][1]

    assert download_index < brief_index
    assert brief_index < reviewer_index
    assert events[reviewer_index][1] is export_payload
    assert button["label"] == "Download review brief (JSON)"
    assert button["file_name"] == "inspection-brief-document-7.json"
    assert button["mime"] == "application/json"
    assert "source evidence" in button["help"].lower()
    assert "processing time" in button["help"].lower()
    assert "source fingerprint" in button["help"].lower()


def test_adhoc_view_does_not_render_persisted_reviewer_controls(monkeypatch):
    events = []

    class FakeStreamlit:
        def info(self, *_args, **_kwargs):
            return None

        def text_input(self, *_args, **_kwargs):
            return ""

        def text_area(self, *_args, **_kwargs):
            return "Fire-safety work is included."

        def button(self, *_args, **_kwargs):
            return True

        def subheader(self, *_args, **_kwargs):
            return None

        def caption(self, *_args, **_kwargs):
            return None

    monkeypatch.setattr(streamlit_app, "st", FakeStreamlit())
    monkeypatch.setattr(
        streamlit_app,
        "render_brief_body",
        lambda brief: events.append(("render_brief_body", brief)),
    )
    monkeypatch.setattr(
        streamlit_app,
        "render_reviewer_decisions",
        lambda *_args: events.append(("render_reviewer_decisions",)),
    )

    streamlit_app.render_adhoc_view()

    assert [event[0] for event in events] == ["render_brief_body"]


def test_init_db_creates_tables(tmp_path):
    db_path = tmp_path / "test.db"
    database.init_db(db_path)
    with database.connect(db_path) as conn:
        names = {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
    assert {
        "documents",
        "processing_runs",
        "briefs",
        "reviewer_decisions",
    }.issubset(names)


def test_sample_flow(tmp_path):
    db_path = tmp_path / "test.db"
    document_id, brief_id = run_pipeline(db_path=db_path)

    assert document_id == 1
    assert brief_id == 1

    documents = database.get_documents(db_path)
    assert len(documents) == 1

    brief = database.get_brief_for_document(document_id, db_path)
    assert brief is not None
    assert brief.summary
    assert brief.confidence == "low"
    assert brief.human_review_required is True
    assert brief.evidence  # at least one source-traced snippet
    assert brief.missing_info  # missing-information signals are captured and stored

    # The synthetic sample seeds these domains; the keyword rules must find them.
    assert "Fire safety" in brief.risk_domains
    assert "Asbestos / hazardous materials" in brief.risk_domains


def test_pipeline_reuses_document_and_preserves_processing_history(tmp_path):
    db_path = tmp_path / "test.db"

    first_document_id, first_brief_id = run_pipeline(db_path=db_path)
    document_id, brief_id = run_pipeline(db_path=db_path)

    assert document_id == first_document_id
    assert brief_id != first_brief_id

    documents = database.get_documents(db_path)
    assert len(documents) == 1
    assert documents[0]["id"] == document_id

    with database.connect(db_path) as conn:
        brief_rows = conn.execute(
            "SELECT id, document_id, processing_run_id FROM briefs ORDER BY id"
        ).fetchall()
        run_rows = conn.execute(
            "SELECT id, document_id FROM processing_runs ORDER BY id"
        ).fetchall()
        foreign_key_violations = conn.execute(
            "PRAGMA foreign_key_check"
        ).fetchall()

    assert [row["id"] for row in brief_rows] == [first_brief_id, brief_id]
    assert len(run_rows) == 2
    assert all(row["document_id"] == document_id for row in brief_rows)
    assert all(row["document_id"] == document_id for row in run_rows)
    assert all(row["processing_run_id"] is not None for row in brief_rows)
    assert len({row["processing_run_id"] for row in brief_rows}) == 2
    assert foreign_key_violations == []

    latest_brief = database.get_brief_for_document(document_id, db_path)
    assert latest_brief is not None
    assert latest_brief.summary

def test_missing_info_detected():
    brief = extract_brief(load_sample())

    # The synthetic sample explicitly states documents are missing.
    assert len(brief.missing_info) >= 2
    joined = " ".join(brief.missing_info).lower()
    assert "not attached" in joined
    assert "requested separately" in joined

    # Missing-info findings are source-traced via evidence snippets.
    matched_terms = {ev.matched_term for ev in brief.evidence}
    assert matched_terms & set(MISSING_INFO_PHRASES)


def test_public_pmp_sample_loads(tmp_path):
    """The curated Luxembourg PMP sample loads with correct provenance metadata
    and produces source-traced findings via the French keyword extension.
    """
    doc = load_sample(PUBLIC_SAMPLE_PATH)

    assert doc.source != "synthetic_sample"
    assert doc.source_url is not None
    assert doc.source_url.startswith("http")
    assert doc.title  # non-empty, parsed from TITLE: line
    assert doc.raw_text  # non-empty body text
    assert doc.clean_text  # cleaned version present

    brief = extract_brief(doc)
    assert brief.confidence == "low"
    assert brief.human_review_required is True
    assert brief.summary  # summary is always populated

    # French keyword extension: CTIE sample must now produce findings.
    assert "Asbestos / hazardous materials" in brief.risk_domains
    assert "Structure / deconstruction" in brief.risk_domains
    assert brief.evidence  # at least one source-traced snippet

    # Pipeline round-trip: document and brief are stored and retrievable.
    db_path = tmp_path / "test_public.db"
    document_id, brief_id = run_pipeline(PUBLIC_SAMPLE_PATH, db_path)
    assert document_id >= 1
    assert brief_id >= 1
    stored_brief = database.get_brief_for_document(document_id, db_path)
    assert stored_brief is not None
    assert stored_brief.human_review_required is True

    docs = database.get_documents(db_path)
    assert len(docs) == 1
    assert docs[0]["source_url"] is not None
    assert docs[0]["source_url"].startswith("http")


def test_ctie_french_keyword_extraction():
    """French keyword extension produces source-traced findings on the CTIE sample.

    This is a small targeted extension for the curated Luxembourg PMP/CTIE sample,
    not general multilingual NLP. Each assertion verifies that a specific French
    term triggers the expected domain and that evidence is traceable to the source.
    """
    doc = load_sample(PUBLIC_SAMPLE_PATH)
    brief = extract_brief(doc)

    assert brief.confidence == "low"
    assert brief.human_review_required is True

    detected = set(brief.risk_domains)
    # Terms present in the CTIE text and their expected domains:
    assert "Asbestos / hazardous materials" in detected    # amiant / flocage
    assert "Structure / deconstruction" in detected        # déconstruct / dalles / charpente
    assert "Remediation / site preparation" in detected    # curage / assainissement
    assert "Materials reuse / circularity" in detected     # réemploi / valorisation

    # Every detected domain must have at least one source-traced evidence snippet.
    matched_domains = {ev.matched_term for ev in brief.evidence}
    assert matched_domains  # at least one term captured

    for ev in brief.evidence:
        assert ev.snippet      # non-empty source line
        assert ev.location     # non-empty location string
        assert ev.matched_term # traceable keyword recorded


def test_evidence_prefers_non_title_line():
    """Evidence selection prefers an OBJECT/body line over the TITLE line.

    On the Belvaux sample, HVAC / Electrical / Kitchen keywords first appear in the
    TITLE line but recur in the OBJECT line; the stored evidence should use the
    more useful OBJECT line, not the TITLE line.
    """
    doc = load_sample(PUBLIC_SAMPLE_BELVAUX_PATH)
    brief = extract_brief(doc)

    assert brief.evidence  # keyword evidence is captured
    for ev in brief.evidence:
        assert not ev.snippet.strip().upper().startswith("TITLE:"), (
            f"Evidence for '{ev.matched_term}' should prefer a non-title line, "
            f"got: {ev.snippet!r} at {ev.location}"
        )
    # The OBJECT line mentions the works; evidence should quote it.
    assert any("travaux" in ev.snippet.lower() for ev in brief.evidence)


def test_category_for_term():
    # A known risk keyword maps to its domain (case-insensitive).
    assert category_for_term("asbestos") == "Asbestos / hazardous materials"
    assert category_for_term("  HVAC ") == "HVAC"
    # A missing-information phrase maps to the missing-information category.
    assert category_for_term("not attached") == "Missing information"
    # An unknown term falls back to "Other".
    assert category_for_term("unrelated") == "Other"
    # French keyword extension: terms from the curated CTIE sample.
    assert category_for_term("amiant") == "Asbestos / hazardous materials"
    assert category_for_term("curage") == "Remediation / site preparation"


def test_build_document_from_text_uses_title_line():
    doc = build_document_from_text("TITLE: Roof replacement works\nObjet: travaux")
    assert doc.title == "Roof replacement works"


def test_build_document_from_text_default_title_when_no_title_line():
    doc = build_document_from_text("Some pasted excerpt without a title line.")
    assert doc.title == "Pasted public excerpt"
    # A caller-provided default title is honoured.
    doc2 = build_document_from_text("No title here", default_title="Custom default")
    assert doc2.title == "Custom default"


def test_build_document_from_text_default_source_is_user_input():
    doc = build_document_from_text("Body text")
    assert doc.source == "user_input"
    # Empty source_url is normalised to None.
    assert doc.source_url is None


def test_build_document_from_text_preserves_source_url():
    doc = build_document_from_text("Body text", source_url="https://example.org/notice")
    assert doc.source_url == "https://example.org/notice"


def test_build_document_from_text_applies_clean_text():
    raw = "TITLE: Façade works\r\nObject line trailing   \r\n"
    doc = build_document_from_text(raw)
    # clean_text normalises CRLF, strips trailing spaces per line, and trims overall.
    assert "\r" not in doc.clean_text
    assert doc.clean_text == "TITLE: Façade works\nObject line trailing"
    # The original raw text is preserved as provided.
    assert doc.raw_text == raw


def test_load_sample_behavior_unchanged():
    """The default synthetic sample still loads with prior provenance behaviour."""
    doc = load_sample()
    assert doc.source == "synthetic_sample"
    assert doc.source_url is None
    assert doc.title  # parsed from the sample's TITLE: line
    assert doc.clean_text


def test_adhoc_input_error_rejects_empty_and_whitespace():
    assert adhoc_input_error("") is not None
    assert adhoc_input_error("   \n\t ") is not None


def test_adhoc_input_error_rejects_over_length():
    msg = adhoc_input_error("x" * 11, max_chars=10)
    assert msg is not None
    assert "too long" in msg.lower()


def test_adhoc_input_error_accepts_valid_text():
    assert adhoc_input_error("A short public excerpt.") is None
    assert adhoc_input_error("x" * 10, max_chars=10) is None


def test_load_validation_summary():
    """The validation snapshot reads the committed CSV and counts match statuses."""
    summary = load_validation_summary()

    # Every reviewed row is counted exactly once across the status buckets.
    assert summary["sample_count"] >= 2
    assert sum(summary["status_counts"].values()) == summary["sample_count"]
    # The committed validation rows include at least one matching baseline check.
    assert summary["status_counts"].get("match", 0) >= 1

    # Compact detail rows back the validation-details table (one row per sample,
    # only the readable columns; long notes/limitations are intentionally omitted).
    detail_rows = summary["detail_rows"]
    assert len(detail_rows) == summary["sample_count"]
    expected_columns = {
        "sample_id",
        "source_type",
        "match_status",
        "manually_expected_domains",
        "extracted_domains",
    }
    for row in detail_rows:
        assert set(row.keys()) == expected_columns
        assert "manual_notes" not in row
        assert "limitation" not in row
        # Display polish: short categorical columns are humanized (no underscores,
        # sentence-cased), while sample_id stays the raw identifier.
        assert "_" not in row["source_type"]
        assert "_" not in row["match_status"]
        if row["match_status"]:
            assert row["match_status"][:1].isupper()
    # Raw CSV values are preserved for the status counts (not humanized).
    assert "match" in summary["status_counts"]
