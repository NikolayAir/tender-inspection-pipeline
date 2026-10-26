# Architecture and data contracts

[Project overview](../README.md) · [User guide](user-guide.md) · [Development](development.md) · [Decision log](decision_log.md)

The Streamlit interface and command-line pipeline share Python extraction and Pydantic models. SQLite stores bundled documents, processing history, generated briefs, and reviewer decisions. The bundled workflow has no runtime dependency on an external API, model provider, or network service.

## Processing paths

The [processing overview in the README](../README.md#how-it-works) shows the shared extraction path, session-only preview, and persisted review and export workflow. The steps below describe the implementation.

The bundled path is coordinated by [`src/pipeline.py`](../src/pipeline.py):

1. [`sample_loader.py`](../src/collect/sample_loader.py) reads a UTF-8 sample, parses source metadata from comment headers, and constructs a `TenderDocument`. Comment lines are excluded from the document text; `SOURCE` and `SOURCE_URL` are mapped to model fields, while other reference metadata stays in the sample file.
2. [`clean.py`](../src/parse/clean.py) normalises Unicode to NFC and line endings to `\n`, trims trailing whitespace on each line, and removes outer whitespace.
3. [`risk_extract.py`](../src/ai/risk_extract.py) extracts an `InspectionBrief` from the normalised text before a write transaction is opened.
4. Inside the transaction, the pipeline finds or creates the logical document and prepares provenance through [`provenance.py`](../src/provenance.py).
5. [`database.py`](../src/db/database.py) saves the document changes, processing run, and linked brief together.
6. [`streamlit_app.py`](../src/app/streamlit_app.py) reads the saved review, displays findings, accepts separate reviewer decisions, and prepares the JSON download.

The pasted-text path constructs an in-memory document and reuses normalisation, extraction, and rendering. It does not persist the excerpt or create downloadable exports.

## Extraction contract

The extractor scans text case-insensitively for declared keyword substrings and maps them to technical domains. It returns a neutral summary, technical scopes, possible review focus areas, information gaps, suggested questions, and evidence. All current extraction results set `confidence` to `low` and `human_review_required` to `true`.

Each detected domain has one evidence snippet. Selection prefers a matching non-title line and falls back to the title when needed. Locations are approximate, one-based lines in the normalised document, not page, section, paragraph, or drawing references.

Information-gap detection matches declared English phrases after flattening whitespace. Its evidence is labelled `source text` rather than assigned a line number. When neither a domain nor an information gap is detected, the brief includes a manual-review message.

Rules do not interpret context, and the taxonomy has limited French construction terminology. [Data and validation](data-and-validation.md) describes the examples and known gaps. Shared model definitions are in [`src/models.py`](../src/models.py).

## Persisted records

| Record | Responsibility | History boundary |
| --- | --- | --- |
| `documents` | Current source record for a logical document | Complete source-text revisions are not preserved |
| `processing_runs` | Timestamp, extractor and schema versions, and source fingerprint for one run | A new record is stored for each persisted run |
| `briefs` | Generated result linked to one exact processing run | Generated briefs remain immutable |
| `reviewer_decisions` | Human-authored state and optional note for one generated target | Every saved change appends an event |

A logical document is identified by `(source, title)`. Explicit processing reuses that document, updates its current source record, and appends a processing run and brief. Earlier briefs and their decisions remain stored; the interface displays the latest brief.

Each processing run records a UTC timestamp, extractor name and version, brief-schema version, and SHA-256 fingerprint of the normalised source text. The fingerprint identifies the processed text but does not preserve earlier source revisions.

Reviewer targets are identified by `brief_id`, `target_type`, and a zero-based `target_index`. Supported target types are `risk_domain` and `missing_info`. States are `accepted`, `rejected`, and `needs_follow_up`. An item with no event is displayed as `Unreviewed` without a stored record. Generated review questions are not decision targets. New briefs start without decisions; earlier decisions are not transferred.

Supported database migrations preserve stored data. Historical briefs without recorded provenance receive explicit legacy metadata rather than inferred current-version values.

## Write transactions

Each sample-processing attempt saves document changes, the processing run, and its brief through one connection and transaction. Failure during extraction, provenance preparation, or persistence preserves the previous source text and metadata, processing history, reviewer decisions, and serialised export.

Atomicity applies per sample: an earlier successful sample remains saved if processing a later one fails. Schema initialisation and supported migrations are separate from processing transactions.

## Recovery and consistent reads

### Automatic sample recovery

Automatic initialisation checks each bundled sample by `(source, title)`. A sample is complete when its stored document has a brief linked to a processing run for that same document, including restored legacy provenance. Unrelated documents do not count towards completeness.

Completed samples are retained even if the bundled source file or extractor version has changed. Once all samples are complete, repeated initialisation adds no records. Refreshing a completed result requires explicit pipeline execution.

Recovery checks completeness before extraction and again inside the write transaction. If another session has completed the sample in the meantime, the later attempt skips the write. Extraction can therefore complete without storing a redundant result. Standalone completeness lookups use read connections without reserving write access.

If recovery fails, the interface keeps readable saved documents, reviews, and downloads available, shows a recovery warning, and retries on refresh. A successful retry retains the selected document. An empty database, unavailable storage, and an unreadable selected review receive separate messages.

### Consistent review reads

The interface reads the selected document, its latest brief, processing metadata, and reviewer-decision history in one SQLite read transaction. The source excerpt, findings, reviewer controls, and JSON download therefore use the same saved result, even if an explicit pipeline run saves a newer result while the page is loading. Standalone exports also read the result and decisions in one transaction.

The read transaction ends before rendering. A page may finish with the earlier result if reprocessing completes after its snapshot begins; the next page rerun reads the latest result. A form submitted for an earlier brief is not applied to a newer brief. These guarantees do not provide reviewer identity, authentication, or simultaneous review editing.

## Versioned JSON export

[`src/exports.py`](../src/exports.py) defines the export models and deterministic serialisation. The interface downloads current schema `1.1.0`. Schema `1.0.0` remains available for explicit compatibility serialisation without reviewer-decision history.

| Field | Contents |
| --- | --- |
| `export_schema_version` | Export envelope version |
| `document` | Stored document ID, source label, source URL, and title |
| `processing_run` | Linked run ID, UTC timestamp, extractor and brief-schema versions, and normalised-source fingerprint |
| `brief_id` | Persisted generated-brief ID |
| `brief` | Complete generated brief, including source evidence |
| `reviewer_decisions` | Schema `1.1.0` only: complete history ordered by strictly increasing event ID |

Each reviewer event contains its ID, target type and index, state, optional note, and UTC decision timestamp. Export-schema, brief-schema, and extractor versions are independent compatibility boundaries.

Serialisation uses sorted keys, two-space indentation, UTF-8-compatible text, and one terminating newline. Serialising the same persisted payload repeatedly produces identical bytes.

Independent runs retain different database identifiers and processing timestamps. The [reproducibility test](../tests/test_reproducibility.py) excludes only `document.id`, `processing_run.id`, `processing_run.processed_at`, and `brief_id`, then deeply compares the remaining stable payload, including evidence ordering. This does not claim byte-identical exports across runs or statistical extraction accuracy.

An export represents one persisted brief and its linked records. It is not a database dump, source-document archive, or complete source-revision history.
