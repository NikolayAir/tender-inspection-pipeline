"""Summary counts describe matches while earlier saved reviews remain intact."""

from contextlib import closing
from datetime import datetime, timezone

import pytest

from src import pipeline
from src.ai.risk_extract import NO_MATCH_NOTICE, extract_brief
from src.collect.sample_loader import build_document_from_text, load_sample
from src.db import database
from src.exports import serialize_brief_export
from src.models import InspectionBrief, ReviewerDecision
from src.provenance import build_processing_run


@pytest.mark.parametrize(
    ("text", "focus_count", "gap_count", "manual_notice"),
    [
        ("General public announcement.", 0, 0, True),
        ("Fire safety works.", 1, 0, False),
        ("Drawings are not attached.", 0, 1, False),
        ("Fire safety works. Drawings are not attached.", 1, 1, False),
        ("Drawings are not attached. The schedule is not included.", 0, 2, False),
    ],
)
def test_summary_counts_matches_without_counting_the_manual_notice(
    text: str, focus_count: int, gap_count: int, manual_notice: bool,
):
    brief = extract_brief(build_document_from_text(text))

    assert f"{focus_count} possible technical review focus area(s) and {gap_count} explicit information gap(s)." in brief.summary
    assert len(brief.risk_domains) == focus_count
    assert (NO_MATCH_NOTICE in brief.missing_info) is manual_notice
    if manual_notice:
        assert brief.missing_info == [NO_MATCH_NOTICE]
        assert brief.evidence == []
        assert brief.review_questions == []
    else:
        assert len(brief.missing_info) == gap_count
    assert brief.confidence == "low"
    assert brief.human_review_required is True


def _stored_history(db_path):
    with closing(database.connect(db_path)) as conn:
        return {
            table: [dict(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY id")]
            for table in ("briefs", "processing_runs", "reviewer_decisions")
        }


def test_summary_correction_preserves_completed_briefs_and_original_notice_target(tmp_path, monkeypatch):
    sample = tmp_path / "unmatched.txt"
    sample.write_text("# SOURCE: synthetic_sample\nTITLE: Plain announcement\nGeneral public announcement.\n", encoding="utf-8")
    db_path = tmp_path / "summary-history.db"
    database.init_db(db_path)
    document = load_sample(sample)
    document_id = database.insert_document(document, db_path)
    old_run = build_processing_run(document_id, document).model_copy(update={"extractor_version": "1.0.0"})
    old_summary = 'For "Plain announcement", the rule-based extractor flagged 0 possible technical review focus area(s) and 1 explicit information gap(s).'
    _, old_brief_id = database.insert_processing_result(
        old_run, InspectionBrief(summary=old_summary, missing_info=[NO_MATCH_NOTICE]), db_path,
    )
    database.insert_reviewer_decision(
        ReviewerDecision(
            brief_id=old_brief_id, target_type="missing_info", target_index=0,
            state="needs_follow_up", note="Read the excerpt manually.",
            decided_at=datetime(2026, 10, 27, 12, tzinfo=timezone.utc),
        ), db_path,
    )
    original = database.get_latest_brief_export(document_id, db_path)
    assert original is not None
    original_json = serialize_brief_export(original)
    before = _stored_history(db_path)
    monkeypatch.setattr(pipeline, "BUNDLED_SAMPLES", [sample])

    assert pipeline.ensure_bundled_samples(db_path) == []
    retained = database.get_latest_brief_export(document_id, db_path)
    assert retained is not None
    assert serialize_brief_export(retained) == original_json
    assert _stored_history(db_path) == before

    new_document_id, new_brief_id = pipeline.run_pipeline(sample, db_path)
    current = database.get_latest_brief_export(document_id, db_path)
    assert current is not None
    assert new_document_id == document_id
    assert new_brief_id != old_brief_id
    assert "0 explicit information gap(s)." in current.brief.summary
    assert current.brief.missing_info == [NO_MATCH_NOTICE]
    assert current.reviewer_decisions == []
    assert current.processing_run.extractor_version == "1.0.1"
    assert current.processing_run.brief_schema_version == "1.0.0"
    assert current.export_schema_version == "1.1.0"
    after = _stored_history(db_path)
    assert after["briefs"][:-1] == before["briefs"]
    assert after["processing_runs"][:-1] == before["processing_runs"]
    assert after["reviewer_decisions"] == before["reviewer_decisions"]
    assert [(event.target_type, event.target_index) for event in retained.reviewer_decisions] == [("missing_info", 0)]
