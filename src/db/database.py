"""SQLite storage layer.

Four tables are maintained:

- ``documents`` stores the current structured source record for each logical
  tender document.
- ``processing_runs`` stores auditable metadata for every persisted extraction
  execution.
- ``briefs`` stores the structured result linked to both its source document
  and exactly one processing run.
- ``reviewer_decisions`` stores append-only human-authored decision events
  linked to generated items in one persisted brief.

List fields and evidence remain JSON text at this project stage. Database
operations accept ``db_path`` parameters so tests can use temporary databases
and never touch ``data/processed/tender_inspection.db``.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager, nullcontext
from datetime import datetime, timezone
from pathlib import Path

from src.exports import (
    ExportDocument,
    ExportProcessingRun,
    ExportReviewerDecision,
    VersionedBriefExport,
)
from src.models import (
    EvidenceSnippet,
    InspectionBrief,
    ProcessingRun,
    ReviewerDecision,
    StoredReviewerDecision,
    TenderDocument,
)
from src.provenance import (
    LEGACY_BRIEF_SCHEMA_VERSION,
    LEGACY_EXTRACTOR_NAME,
    LEGACY_EXTRACTOR_VERSION,
    source_content_fingerprint,
)

DEFAULT_DB_PATH = Path("data") / "processed" / "tender_inspection.db"


def connect(db_path: Path | str = DEFAULT_DB_PATH) -> sqlite3.Connection:
    """Open a SQLite connection, creating the parent folder if needed."""
    path = Path(db_path)
    if path.parent and not path.parent.exists():
        path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def transaction(
    db_path: Path | str = DEFAULT_DB_PATH,
    *,
    connection: sqlite3.Connection | None = None,
) -> Iterator[sqlite3.Connection]:
    """Join a supplied transaction or own and close a new write transaction.

    Only the owner commits or rolls back. Reserve the SQLite writer before any
    document lookup so its identity and all related writes share one transaction.
    """
    if connection is not None:
        yield connection
        return

    with closing(connect(db_path)) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        yield conn


def _required_lastrowid(cursor: sqlite3.Cursor) -> int:
    """Return an inserted row id or fail if SQLite did not provide one."""
    lastrowid = cursor.lastrowid
    if lastrowid is None:
        raise RuntimeError("SQLite insert did not return a row id")
    return lastrowid


def init_db(db_path: Path | str = DEFAULT_DB_PATH) -> None:
    """Create or additively upgrade the SQLite schema.

    Existing document and brief rows are retained. Each historical brief
    without provenance receives one truthful legacy processing-run record.
    Repeated initialisation does not duplicate backfilled runs.
    """
    with closing(connect(db_path)) as conn, conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS documents (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                source      TEXT NOT NULL,
                source_url  TEXT,
                title       TEXT NOT NULL,
                raw_text    TEXT NOT NULL,
                clean_text  TEXT NOT NULL,
                created_at  TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS processing_runs (
                id                         INTEGER PRIMARY KEY AUTOINCREMENT,
                document_id                INTEGER NOT NULL REFERENCES documents(id),
                processed_at               TEXT NOT NULL,
                extractor_name             TEXT NOT NULL,
                extractor_version          TEXT NOT NULL,
                brief_schema_version       TEXT NOT NULL,
                source_content_fingerprint TEXT NOT NULL
            )
            """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS briefs (
                id                    INTEGER PRIMARY KEY AUTOINCREMENT,
                document_id           INTEGER NOT NULL REFERENCES documents(id),
                processing_run_id     INTEGER NOT NULL REFERENCES processing_runs(id),
                summary               TEXT NOT NULL,
                technical_scopes      TEXT NOT NULL DEFAULT '[]',
                risk_domains          TEXT NOT NULL DEFAULT '[]',
                missing_info          TEXT NOT NULL DEFAULT '[]',
                review_questions      TEXT NOT NULL DEFAULT '[]',
                evidence              TEXT NOT NULL DEFAULT '[]',
                confidence            TEXT NOT NULL DEFAULT 'low',
                human_review_required INTEGER NOT NULL DEFAULT 1,
                created_at            TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS reviewer_decisions (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                brief_id     INTEGER NOT NULL REFERENCES briefs(id),
                target_type  TEXT NOT NULL
                    CHECK (target_type IN ('risk_domain', 'missing_info')),
                target_index INTEGER NOT NULL
                    CHECK (target_index >= 0),
                state        TEXT NOT NULL
                    CHECK (
                        state IN (
                            'accepted',
                            'rejected',
                            'needs_follow_up'
                        )
                    ),
                note         TEXT
                    CHECK (
                        note IS NULL
                        OR length(note) BETWEEN 1 AND 2000
                    ),
                decided_at   TEXT NOT NULL
            )
            """
        )

        brief_columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(briefs)").fetchall()
        }
        if "processing_run_id" not in brief_columns:
            conn.execute(
                """
                ALTER TABLE briefs
                ADD COLUMN processing_run_id
                    INTEGER REFERENCES processing_runs(id)
                """
            )

        _backfill_legacy_processing_runs(conn)

        unlinked_count = conn.execute(
            """
            SELECT COUNT(*) AS count
            FROM briefs
            WHERE processing_run_id IS NULL
            """
        ).fetchone()["count"]

        if unlinked_count:
            raise RuntimeError(
                f"Database contains {unlinked_count} brief(s) "
                "without processing provenance"
            )

        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_processing_runs_document_id
            ON processing_runs(document_id)
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_briefs_document_id
            ON briefs(document_id)
            """
        )
        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_briefs_processing_run_id
            ON briefs(processing_run_id)
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_reviewer_decisions_brief_id
            ON reviewer_decisions(brief_id)
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_reviewer_decisions_target_history
            ON reviewer_decisions(
                brief_id,
                target_type,
                target_index,
                id
            )
            """
        )


def _backfill_legacy_processing_runs(conn: sqlite3.Connection) -> None:
    """Create one truthful legacy processing run per unlinked historical brief."""
    rows = conn.execute(
        """
        SELECT
            b.id AS brief_id,
            b.document_id,
            b.created_at,
            d.clean_text
        FROM briefs AS b
        JOIN documents AS d
          ON d.id = b.document_id
        WHERE b.processing_run_id IS NULL
        ORDER BY b.id
        """
    ).fetchall()

    for row in rows:
        run_cursor = conn.execute(
            """
            INSERT INTO processing_runs (
                document_id,
                processed_at,
                extractor_name,
                extractor_version,
                brief_schema_version,
                source_content_fingerprint
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                row["document_id"],
                row["created_at"],
                LEGACY_EXTRACTOR_NAME,
                LEGACY_EXTRACTOR_VERSION,
                LEGACY_BRIEF_SCHEMA_VERSION,
                source_content_fingerprint(row["clean_text"]),
            ),
        )
        processing_run_id = _required_lastrowid(run_cursor)

        update_cursor = conn.execute(
            """
            UPDATE briefs
            SET processing_run_id = ?
            WHERE id = ? AND processing_run_id IS NULL
            """,
            (processing_run_id, row["brief_id"]),
        )

        if update_cursor.rowcount != 1:
            raise RuntimeError(
                f"Could not link legacy brief {row['brief_id']} "
                "to its processing run"
            )


def insert_document(
    document: TenderDocument,
    db_path: Path | str = DEFAULT_DB_PATH,
    *,
    connection: sqlite3.Connection | None = None,
) -> int:
    """Insert a document and return its new id."""
    with transaction(db_path, connection=connection) as conn:
        cursor = conn.execute(
            """
            INSERT INTO documents (source, source_url, title, raw_text, clean_text)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                document.source,
                document.source_url,
                document.title,
                document.raw_text,
                document.clean_text,
            ),
        )
        return _required_lastrowid(cursor)


def find_document_id(
    source: str,
    title: str,
    db_path: Path | str = DEFAULT_DB_PATH,
    *,
    connection: sqlite3.Connection | None = None,
) -> int | None:
    """Return an existing document id for this source and title."""
    with (
        nullcontext(connection)
        if connection is not None
        else closing(connect(db_path))
    ) as conn:
        row = conn.execute(
            """
            SELECT id
            FROM documents
            WHERE source = ? AND title = ?
            ORDER BY id
            LIMIT 1
            """,
            (source, title),
        ).fetchone()

    return int(row["id"]) if row is not None else None


def has_completed_result(
    source: str,
    title: str,
    db_path: Path | str = DEFAULT_DB_PATH,
    *,
    connection: sqlite3.Connection | None = None,
) -> bool:
    """Check whether this identity has a brief linked to its own processing run.

    Use the same document lookup as ``find_document_id``. Restored legacy runs
    count as complete; source-content and extractor-version changes do not
    trigger automatic reprocessing. A supplied connection keeps this check in
    the caller's transaction without committing, rolling back or closing it.
    """
    with (
        nullcontext(connection)
        if connection is not None
        else closing(connect(db_path))
    ) as conn:
        row = conn.execute(
            """
            SELECT EXISTS (
                SELECT 1
                FROM briefs AS b
                JOIN processing_runs AS pr
                  ON pr.id = b.processing_run_id
                 AND pr.document_id = b.document_id
                WHERE b.document_id = (
                    SELECT id
                    FROM documents
                    WHERE source = ? AND title = ?
                    ORDER BY id
                    LIMIT 1
                )
            ) AS complete
            """,
            (source, title),
        ).fetchone()

    return bool(row["complete"])


def update_document(
    document_id: int,
    document: TenderDocument,
    db_path: Path | str = DEFAULT_DB_PATH,
    *,
    connection: sqlite3.Connection | None = None,
) -> None:
    """Refresh an existing document so reruns reflect source-file edits."""
    with transaction(db_path, connection=connection) as conn:
        conn.execute(
            """
            UPDATE documents
            SET source_url = ?, raw_text = ?, clean_text = ?
            WHERE id = ?
            """,
            (
                document.source_url,
                document.raw_text,
                document.clean_text,
                document_id,
            ),
        )


def insert_processing_result(
    processing_run: ProcessingRun,
    brief: InspectionBrief,
    db_path: Path | str = DEFAULT_DB_PATH,
    *,
    connection: sqlite3.Connection | None = None,
) -> tuple[int, int]:
    """Atomically persist one processing run and its linked brief.

    Returns ``(processing_run_id, brief_id)``. Failure of either insert rolls
    back both records. With a supplied connection, the caller owns the transaction.
    """
    processed_at = processing_run.processed_at
    if processed_at.tzinfo is None or processed_at.utcoffset() is None:
        raise ValueError("processing_run.processed_at must be timezone-aware")

    processed_at_utc = processed_at.astimezone(timezone.utc).isoformat()

    with transaction(db_path, connection=connection) as conn:
        run_cursor = conn.execute(
            """
            INSERT INTO processing_runs (
                document_id,
                processed_at,
                extractor_name,
                extractor_version,
                brief_schema_version,
                source_content_fingerprint
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                processing_run.document_id,
                processed_at_utc,
                processing_run.extractor_name,
                processing_run.extractor_version,
                processing_run.brief_schema_version,
                processing_run.source_content_fingerprint,
            ),
        )
        processing_run_id = _required_lastrowid(run_cursor)

        brief_cursor = conn.execute(
            """
            INSERT INTO briefs (
                document_id,
                processing_run_id,
                summary,
                technical_scopes,
                risk_domains,
                missing_info,
                review_questions,
                evidence,
                confidence,
                human_review_required
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                processing_run.document_id,
                processing_run_id,
                brief.summary,
                json.dumps(brief.technical_scopes),
                json.dumps(brief.risk_domains),
                json.dumps(brief.missing_info),
                json.dumps(brief.review_questions),
                json.dumps([item.model_dump() for item in brief.evidence]),
                brief.confidence,
                int(brief.human_review_required),
            ),
        )
        brief_id = _required_lastrowid(brief_cursor)

    return processing_run_id, brief_id


def insert_reviewer_decision(
    decision: ReviewerDecision,
    db_path: Path | str = DEFAULT_DB_PATH,
) -> int:
    """Append one validated reviewer-decision event and return its id."""
    decided_at_utc = decision.decided_at.astimezone(timezone.utc).isoformat()

    with connect(db_path) as conn:
        brief_row = conn.execute(
            """
            SELECT risk_domains, missing_info
            FROM briefs
            WHERE id = ?
            """,
            (decision.brief_id,),
        ).fetchone()

        if brief_row is None:
            raise ValueError(
                f"brief_id {decision.brief_id} "
                "does not reference a persisted brief"
            )

        target_column = (
            "risk_domains"
            if decision.target_type == "risk_domain"
            else "missing_info"
        )
        target_values = json.loads(brief_row[target_column])

        if not isinstance(target_values, list):
            raise RuntimeError(
                f"Persisted {target_column} value is not a list "
                f"for brief {decision.brief_id}"
            )

        if decision.target_index >= len(target_values):
            raise ValueError(
                f"target_index {decision.target_index} is invalid for "
                f"{decision.target_type} on brief {decision.brief_id}"
            )

        cursor = conn.execute(
            """
            INSERT INTO reviewer_decisions (
                brief_id,
                target_type,
                target_index,
                state,
                note,
                decided_at
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                decision.brief_id,
                decision.target_type,
                decision.target_index,
                decision.state,
                decision.note,
                decided_at_utc,
            ),
        )

        return _required_lastrowid(cursor)


def _reviewer_decision_from_row(
    row: sqlite3.Row,
) -> StoredReviewerDecision:
    """Build a validated reviewer-decision event from a database row."""
    return StoredReviewerDecision(
        id=row["id"],
        brief_id=row["brief_id"],
        target_type=row["target_type"],
        target_index=row["target_index"],
        state=row["state"],
        note=row["note"],
        decided_at=_stored_timestamp_as_utc(row["decided_at"]),
    )


def get_reviewer_decision_history(
    brief_id: int,
    db_path: Path | str = DEFAULT_DB_PATH,
) -> list[StoredReviewerDecision]:
    """Return decision history in deterministic insertion order."""
    with closing(connect(db_path)) as conn:
        return _get_reviewer_decision_history(brief_id, conn)


def _get_reviewer_decision_history(
    brief_id: int,
    conn: sqlite3.Connection,
) -> list[StoredReviewerDecision]:
    """Read decision events through the supplied connection."""
    rows = conn.execute(
        """
        SELECT
            id,
            brief_id,
            target_type,
            target_index,
            state,
            note,
            decided_at
        FROM reviewer_decisions
        WHERE brief_id = ?
        ORDER BY id
        """,
        (brief_id,),
    ).fetchall()

    return [_reviewer_decision_from_row(row) for row in rows]


def get_latest_reviewer_decisions(
    brief_id: int,
    db_path: Path | str = DEFAULT_DB_PATH,
) -> dict[tuple[str, int], StoredReviewerDecision]:
    """Return the latest appended event for every reviewed target."""
    latest: dict[tuple[str, int], StoredReviewerDecision] = {}

    for decision in get_reviewer_decision_history(brief_id, db_path):
        latest[(decision.target_type, decision.target_index)] = decision

    return latest


def get_documents(db_path: Path | str = DEFAULT_DB_PATH) -> list[dict]:
    """Return all documents as dictionaries, newest first."""
    with connect(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM documents ORDER BY id DESC"
        ).fetchall()
        return [dict(row) for row in rows]


def _inspection_brief_from_row(row: sqlite3.Row) -> InspectionBrief:
    """Build a validated inspection brief from persisted JSON columns."""
    return InspectionBrief(
        summary=row["summary"],
        technical_scopes=json.loads(row["technical_scopes"]),
        risk_domains=json.loads(row["risk_domains"]),
        missing_info=json.loads(row["missing_info"]),
        review_questions=json.loads(row["review_questions"]),
        evidence=[
            EvidenceSnippet(**item)
            for item in json.loads(row["evidence"])
        ],
        confidence=row["confidence"],
        human_review_required=bool(row["human_review_required"]),
    )


def _stored_timestamp_as_utc(value: str) -> datetime:
    """Parse a stored timestamp, treating legacy SQLite timestamps as UTC."""
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def get_brief_for_document(
    document_id: int,
    db_path: Path | str = DEFAULT_DB_PATH,
) -> InspectionBrief | None:
    """Return the most recently persisted brief for a document."""
    with connect(db_path) as conn:
        row = conn.execute(
            """
            SELECT b.*
            FROM briefs AS b
            JOIN processing_runs AS pr
              ON pr.id = b.processing_run_id
            WHERE b.document_id = ?
            ORDER BY pr.id DESC, b.id DESC
            LIMIT 1
            """,
            (document_id,),
        ).fetchone()

    return _inspection_brief_from_row(row) if row is not None else None


def get_latest_brief_export(
    document_id: int,
    db_path: Path | str = DEFAULT_DB_PATH,
) -> VersionedBriefExport | None:
    """Read the latest brief, metadata and decisions from one SQLite snapshot."""
    with closing(connect(db_path)) as conn, conn:
        conn.execute("BEGIN")
        return _get_latest_brief_export(document_id, conn)


def get_document_review(
    document_id: int,
    db_path: Path | str = DEFAULT_DB_PATH,
) -> tuple[dict, VersionedBriefExport | None] | None:
    """Read a source document and its latest review from one SQLite snapshot.

    Return None if the document is absent, or (document, None) if it has no
    brief linked to its own processing run. The read transaction ends before
    the page is rendered, without reserving write access.
    """
    with closing(connect(db_path)) as conn, conn:
        # Keep source, brief and decisions in one read transaction; separate queries
        # could otherwise see different committed database states.
        conn.execute("BEGIN")
        document = conn.execute(
            "SELECT * FROM documents WHERE id = ?", (document_id,),
        ).fetchone()
        if document is None:
            return None

        return dict(document), _get_latest_brief_export(document_id, conn)


def _get_latest_brief_export(
    document_id: int,
    conn: sqlite3.Connection,
) -> VersionedBriefExport | None:
    """Read a result and its decisions within an existing read transaction."""
    row = conn.execute(
        """
        SELECT
            d.id AS document_id,
            d.source AS document_source,
            d.source_url AS document_source_url,
            d.title AS document_title,
            pr.id AS processing_run_id,
            pr.processed_at,
            pr.extractor_name,
            pr.extractor_version,
            pr.brief_schema_version,
            pr.source_content_fingerprint,
            b.id AS brief_id,
            b.summary,
            b.technical_scopes,
            b.risk_domains,
            b.missing_info,
            b.review_questions,
            b.evidence,
            b.confidence,
            b.human_review_required
        FROM briefs AS b
        JOIN documents AS d
          ON d.id = b.document_id
        JOIN processing_runs AS pr
          ON pr.id = b.processing_run_id
         AND pr.document_id = b.document_id
        WHERE b.document_id = ?
        ORDER BY pr.id DESC, b.id DESC
        LIMIT 1
        """,
        (document_id,),
    ).fetchone()

    if row is None:
        return None

    reviewer_decisions = [
        ExportReviewerDecision(
            id=decision.id,
            target_type=decision.target_type,
            target_index=decision.target_index,
            state=decision.state,
            note=decision.note,
            decided_at=decision.decided_at,
        )
        for decision in _get_reviewer_decision_history(
            row["brief_id"],
            conn,
        )
    ]

    return VersionedBriefExport(
        document=ExportDocument(
            id=row["document_id"],
            source=row["document_source"],
            source_url=row["document_source_url"],
            title=row["document_title"],
        ),
        processing_run=ExportProcessingRun(
            id=row["processing_run_id"],
            processed_at=_stored_timestamp_as_utc(row["processed_at"]),
            extractor_name=row["extractor_name"],
            extractor_version=row["extractor_version"],
            brief_schema_version=row["brief_schema_version"],
            source_content_fingerprint=row["source_content_fingerprint"],
        ),
        brief_id=row["brief_id"],
        brief=_inspection_brief_from_row(row),
        reviewer_decisions=reviewer_decisions,
    )
