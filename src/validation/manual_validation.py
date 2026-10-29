"""Read the committed qualitative manual-validation CSV for display.

Display-only transparency: this reads the existing
``data/labels/manual_validation_v1.csv`` and does not run, change, or
re-evaluate the extraction logic. Kept out of the Streamlit UI file so the
validation/data logic can be tested without importing Streamlit.
"""

from __future__ import annotations

import csv
from collections import Counter
from pathlib import Path

# Anchor the bundled CSV to the repository, independently of the process's working directory.
ROOT = Path(__file__).resolve().parents[2]

VALIDATION_CSV_PATH = ROOT / "data" / "labels" / "manual_validation_v1.csv"

def humanize_validation_label(value: str) -> str:
    """Make a CSV token readable: underscores -> spaces, sentence-cased.

    Display-only formatting (e.g. ``real_public_curated`` -> ``Real public curated``,
    ``match`` -> ``Match``). It does not alter the raw CSV values used for counts.
    """
    return value.replace("_", " ").strip().capitalize()

def load_validation_summary(csv_path: Path = VALIDATION_CSV_PATH) -> dict:
    """Summarise the committed qualitative manual-validation CSV (standard library only).

    Returns the number of manually reviewed samples and a mapping of
    ``match_status`` -> count. This is display-only transparency: it reads the
    existing ``data/labels/manual_validation_v1.csv`` and does not run, change, or
    re-evaluate the extraction logic.
    """
    with Path(csv_path).open(encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    status_counts = Counter((row.get("match_status") or "").strip() for row in rows)
    detail_columns = (
        "sample_id",
        "source_type",
        "match_status",
        "manually_expected_domains",
        "extracted_domains",
    )
    detail_rows = []
    for row in rows:
        detail = {col: (row.get(col) or "").strip() for col in detail_columns}
        # Keep sample_id raw; humanize only the short categorical columns for display.
        detail["source_type"] = humanize_validation_label(detail["source_type"])
        detail["match_status"] = humanize_validation_label(detail["match_status"])
        detail_rows.append(detail)
    return {
        "sample_count": len(rows),
        "status_counts": dict(status_counts),
        "detail_rows": detail_rows,
    }
