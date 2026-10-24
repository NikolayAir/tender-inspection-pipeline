# Tender-to-Inspection Brief

Tender-to-Inspection Brief is a reviewer-assistance application that turns public construction tender notices and excerpts into structured, source-traced technical review briefs.

It uses transparent, deterministic keyword rules to highlight technical scopes, information gaps, and follow-up questions. Reviewers verify generated findings against source evidence, record decisions for persisted findings, and download versioned JSON exports.

The application supports human technical review; it does not make legal, regulatory, compliance, safety, or engineering decisions.

**Live application:** [tender-inspection.streamlit.app](https://tender-inspection.streamlit.app)

**Stack:** Python · Streamlit · SQLite · Pydantic

**Verification:** pytest · Ruff · GitHub Actions

![Tender-to-Inspection Brief overview showing a persisted public tender excerpt, generated review brief, and JSON download](docs/assets/tender-inspection-overview.png)

## Key capabilities

- generate structured review briefs from bundled public or synthetic excerpts, and preview pasted public text for the current session only;
- link each detected technical domain to source-labelled evidence, while keeping information gaps and suggested reviewer questions separate;
- persist documents, processing runs, and immutable generated briefs in SQLite with explicit provenance and preserved processing history;
- record reviewer decisions and optional notes for generated focus areas and information gaps, preserving every saved change in an append-only history;
- export a persisted brief with its processing provenance and complete ordered reviewer-decision history as deterministic, versioned JSON;
- run the bundled workflow offline without API keys, model providers, or runtime network services;
- check expected behaviour with automated tests and a committed qualitative validation set.

## Reviewer workflow

1. Select one of the persisted bundled samples or paste a short public, non-confidential excerpt for a session-only preview.
2. Inspect the generated summary, technical scopes, potential review focus areas, information gaps, and suggested questions.
3. Verify each generated finding against its source-labelled evidence.
4. For persisted bundled briefs, save `Accepted`, `Rejected`, or `Needs follow-up` decisions with optional notes. Each update appends a new history event rather than changing the generated brief.
5. Download the persisted brief as versioned JSON.

The reviewer remains responsible for interpreting the source material and deciding what requires follow-up.

![Reviewer-decision controls for a persisted generated finding, showing the current state, an optional note, and a new decision form](docs/assets/tender-inspection-reviewer-decisions.png)

## How it works

```mermaid
flowchart TD
    bundled["Bundled sample"] --> processing["Normalise and extract"]
    pasted["Pasted public excerpt"] --> processing
    processing --> brief["InspectionBrief<br/>findings + source evidence"]

    brief -->|Pasted path| preview["Session-only preview"]
    brief -->|Bundled path| persistence["SQLite persistence<br/>document + run + brief"]
    persistence --> decisions["SQLite: append-only<br/>reviewer decisions"]
    persistence --> export["Versioned JSON export<br/>brief + provenance + decision history"]
    decisions --> export
```

`src/pipeline.py` coordinates the bundled processing path:

1. `src/collect/sample_loader.py` loads a bundled sample and parses its source metadata.
2. `src/parse/clean.py` normalises whitespace and encoding.
3. `src/ai/risk_extract.py` applies the deterministic keyword-to-domain baseline and returns an `InspectionBrief`.
4. `src/provenance.py` records the UTC processing time, extractor and brief-schema versions, and a SHA-256 fingerprint of the normalised source text.
5. Document changes, the processing run, and its linked brief are saved through one SQLite connection and transaction. A failed attempt leaves the previously stored source and results unchanged.
6. The Streamlit interface displays the latest persisted brief, accepts separate reviewer decisions, and prepares the JSON export.

Material architecture and product decisions, including their trade-offs, are recorded in [`docs/decision_log.md`](docs/decision_log.md).

## Run locally

From the repository root, create and activate a virtual environment, then install the runtime dependencies:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

Run the bundled pipeline and start the application:

```bash
python -m src.pipeline
python -m streamlit run src/app/streamlit_app.py
```

The pipeline creates or updates `data/processed/tender_inspection.db`. The application automatically recovers missing or incomplete bundled samples while retaining completed results and their reviewer decisions.

Process one sample explicitly:

```bash
python -m src.pipeline \
  --sample data/samples/synthetic_sample_tender_001.txt
```

## Verification

Install the development dependencies and run the local verification commands:

```bash
python -m pip install -r requirements-dev.txt
python -m ruff check .
python -m compileall -q src tests
python -m src.pipeline
python -m pytest -q
git diff --check
```

GitHub Actions runs Ruff lint checks, compilation, the bundled-sample pipeline, and pytest on each supported Python version for pull requests and pushes to `main`.

## Processing and review boundaries

### Generated briefs

The extractor scans normalised text case-insensitively and maps declared terms to technical review domains. Each `InspectionBrief` contains a neutral summary, technical scopes, potential review focus areas, information gaps, suggested questions, and matched source snippets with approximate line locations. The current extraction path sets `confidence` to `low` and `human_review_required` to `true`.

The rule-based approach is transparent and reproducible, but it does not interpret context or provide broad multilingual coverage. A small targeted set of French construction terms supports the bundled Luxembourg examples.

### Persistence and reviewer decisions

| Record | Responsibility | History boundary |
| --- | --- | --- |
| `documents` | Current source record for a logical document | Complete source-text revisions are not preserved |
| `processing_runs` | Timestamp, extractor and schema versions, and source fingerprint for one run | A new record is stored for each persisted run |
| `briefs` | Structured generated result linked to one exact processing run | Generated briefs remain immutable |
| `reviewer_decisions` | Human-authored state and optional note for one generated target | Every saved change appends a new event |

Each sample-processing attempt saves its document changes, processing run and generated brief together, or rolls them all back. A failure during extraction, provenance preparation or persistence preserves the previous source text, document metadata, processing history, reviewer decisions and serialised export. Each sample has its own transaction; schema initialisation and supported migrations remain separate.

Automatic initialisation checks each bundled sample by its `(source, title)` identity. A sample is complete when its stored document has a brief linked to a processing run for that same document, including restored legacy provenance. Unrelated documents do not count towards completeness. Completed samples are retained even when the bundled source file or extractor version has changed, and repeated initialisation adds no records once all samples are complete.

Explicit pipeline execution refreshes the selected samples, reuses documents with the same identity, and appends new processing runs and briefs. Earlier reviewer decisions stay attached to their original briefs; each new brief starts without decisions.

Reviewer decisions apply only to generated technical focus areas (`risk_domain`) and information gaps (`missing_info`) in persisted bundled briefs; generated review questions are not decision targets. An item with no saved decision is displayed as `Unreviewed` and does not create a database record.

Existing databases are upgraded without removing stored data. Historical briefs without recorded provenance receive explicit legacy metadata rather than inferred current-version values.

### Session-only excerpts

The **Analyse a public excerpt** tab reuses the cleaning, extraction, and brief-rendering path for short pasted text. Pasted content is processed only in the current session and is not persisted or exported. The application does not fetch supplied URLs, scrape websites, parse PDFs, or call an external model or API.

## Versioned JSON export

The **Download review brief (JSON)** action is available for persisted bundled briefs and uses the current export schema, `1.1.0`. Schema `1.0.0` remains available for explicit compatibility serialisation without reviewer-decision history.

Each export combines stored document metadata, its exact linked processing run, the complete generated brief and source evidence, and the full ordered reviewer-decision history. The export schema, structured brief schema, and extractor version are independent compatibility boundaries.

Serialisation uses sorted keys, two-space indentation, UTF-8-compatible text, and one terminating newline, making repeated serialisation of the same persisted payload byte-for-byte identical. Exports from independent runs differ in database identifiers and processing timestamps; the reproducibility test excludes only those run-specific fields and deeply compares the remaining stable content.

An export represents one persisted brief and its linked records, not a database dump, source-document archive, or complete document-revision history.

## Data sources and validation

The repository contains three samples under `data/samples/`, all of which can be processed without network access.

| File | Type | Coverage |
| --- | --- | --- |
| `synthetic_sample_tender_001.txt` | Synthetic fixture | Deterministic offline testing; not a real tender |
| `public_lu_pmp_ctie_001.txt` | Curated public excerpt | Luxembourg public notice 217578-2026; asbestos remediation and selective deconstruction |
| `public_lu_pmp_snhbm_belvaux_001.txt` | Curated public excerpt | Luxembourg PMP reference 2601359; heating, ventilation, electrical, and kitchen works |

The public samples are short manually curated excerpts from public Luxembourg procurement consultations. No scraping was performed and no full tender dossier was committed. Source URLs and reference metadata are retained in the sample headers.

The qualitative validation set at `data/labels/manual_validation_v1.csv` records manually expected domains, the extractor output at validation time, match status, taxonomy gaps, and known limitations. The regression test checks that the current extractor output continues to match the output recorded in the CSV.

All three samples currently match their declared expected domains. Here, a `match` means agreement with the declared expectations for that sample; it is not a statistical accuracy measure. The dataset is too small to support precision, recall, F1, or broad language-coverage claims.

![Source-traced evidence and qualitative validation for the bundled samples](docs/assets/tender-inspection-evidence-validation.png)

## Current limitations

- Extraction uses a deterministic keyword-to-domain baseline, does not interpret context, and cannot detect domains or paraphrases outside its declared taxonomy.
- French support is limited to terminology used by the bundled samples; French missing-information signals are not detected.
- Evidence locations are approximate line references rather than page, section, paragraph, or drawing references.
- The three-sample qualitative validation set supports regression checks, not statistical accuracy claims.
- Processing history is preserved, but complete historical source-text revisions are not.
- Reviewer decisions apply only to generated focus areas and information gaps in persisted bundled briefs; reviewer identity, authentication, simultaneous editing, and session-only excerpt decisions are not implemented.
- PDF ingestion, URL fetching, scraping, external-service integration, and model-based extraction are not implemented.

The application does not provide document management, BIM coordination, site-inspection records, defect tracking, or regulatory assessment.

## Security

Report suspected vulnerabilities through GitHub Private Vulnerability Reporting, not a public issue. See [`SECURITY.md`](SECURITY.md) for details.

## Releases

See [GitHub Releases](https://github.com/NikolayAir/tender-inspection-pipeline/releases) for versioned release notes.

## License

Tender-to-Inspection Brief is available under the [MIT License](LICENSE).

The MIT License applies to this repository's original source code and documentation. Curated public procurement excerpts under `data/samples/` are not covered by the MIT License and retain their recorded source attribution.
