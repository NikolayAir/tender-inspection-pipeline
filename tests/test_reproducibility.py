"""Integration tests for reproducible persisted review-brief output."""

from __future__ import annotations

from typing import Any

from src.collect.sample_loader import DEFAULT_SAMPLE_PATH
from src.db import database
from src.exports import VersionedBriefExport
from src.pipeline import PUBLIC_SAMPLE_PATH, run_pipeline


def _stable_export_payload(
    exported: VersionedBriefExport,
) -> dict[str, Any]:
    """Return the export content expected to remain stable across runs."""
    payload = exported.model_dump(mode="json")

    # Ignore only run-specific IDs and time; evidence, versions and all other
    # content must still agree. This compares content, not identical export bytes.
    payload["document"].pop("id")
    payload["processing_run"].pop("id")
    payload["processing_run"].pop("processed_at")
    payload.pop("brief_id")

    return payload


def test_independent_runs_produce_equivalent_stable_exports(tmp_path) -> None:
    """The same bundled source produces equivalent stable exported content."""
    first_db_path = tmp_path / "first_run.db"
    second_db_path = tmp_path / "second_run.db"

    first_document_id, _ = run_pipeline(
        DEFAULT_SAMPLE_PATH,
        first_db_path,
    )

    # Seed the second database with a different sample so database-assigned
    # document, processing-run, and brief identifiers cannot match by accident.
    run_pipeline(PUBLIC_SAMPLE_PATH, second_db_path)
    second_document_id, _ = run_pipeline(
        DEFAULT_SAMPLE_PATH,
        second_db_path,
    )

    first_export = database.get_latest_brief_export(
        first_document_id,
        first_db_path,
    )
    second_export = database.get_latest_brief_export(
        second_document_id,
        second_db_path,
    )

    assert first_export is not None
    assert second_export is not None

    assert first_export.document.id != second_export.document.id
    assert first_export.processing_run.id != second_export.processing_run.id
    assert first_export.brief_id != second_export.brief_id

    first_stable_payload = _stable_export_payload(first_export)
    second_stable_payload = _stable_export_payload(second_export)

    assert (
        first_stable_payload["brief"]["evidence"]
        == second_stable_payload["brief"]["evidence"]
    )
    assert first_stable_payload == second_stable_payload
