# User guide

[Project overview](../README.md) · [Architecture and data contracts](architecture.md) · [Data and validation](data-and-validation.md)

Tender-to-Inspection Brief provides two review paths:

| Path | Input | Saved results | Reviewer decisions | JSON download |
| --- | --- | --- | --- | --- |
| **Bundled samples** | Three committed public or synthetic excerpts | SQLite | Supported | Supported |
| **Analyse a public excerpt** | Pasted public, non-confidential text | Session-only preview | Unavailable | Unavailable |

## Review a bundled sample

1. Open **Bundled samples** and select a document. Synthetic fixtures are identified as test data; public excerpts show their recorded source URL.
2. Read the source excerpt and generated summary, technical scopes, review focus areas, information gaps, and suggested questions.
3. Check the evidence table. Each entry includes a matched term, source snippet, and approximate location. A match identifies a possible review topic; the reviewer determines what needs follow-up.
4. Record decisions for the generated focus areas and information gaps.
5. Use **Download review brief (JSON)** to save the result with its processing provenance and decision history.

Suggested questions are prompts for the reviewer, not conclusions about the tender. The current extractor always returns `confidence="low"` and `human_review_required=true`.

### Record decisions

For each generated focus area or information gap, select **Accepted**, **Rejected**, or **Needs follow-up**, optionally add a note, and save. Notes are limited to 2,000 characters. Items without a saved decision appear as **Unreviewed**; displaying that state does not create a database record.

Each save appends an event. The latest event determines the displayed state, and earlier states and notes remain in the history. Saving a decision does not alter the generated brief. Generated questions are not separate decision targets.

Decisions belong to the specific brief being reviewed. If an explicit pipeline run creates a new brief, earlier decisions remain attached to the earlier brief and are not copied to the new one. A form submitted for an earlier brief is not applied to a newer brief.

## Preview a public excerpt

1. Open **Analyse a public excerpt**.
2. Paste non-empty public, non-confidential text, up to 50,000 characters. A document title and source URL are optional; the URL is reference metadata and is not fetched. A `TITLE:` line in the text takes precedence over the optional title field.
3. Select **Generate review brief**, then inspect the findings and evidence.

This path uses the same text cleaning, extraction, and brief display as the bundled workflow. Pasted content is processed only in the current session; it is not written to SQLite or exported. The application does not scrape websites, parse PDFs, or call an external model or API.

The bundled validation results do not establish extraction quality for pasted text. See [data and validation](data-and-validation.md) for the tested examples and limitations.

## Saved results and recovery

The application automatically processes bundled samples whose stored results are missing or incomplete. Completed results and their decisions are retained, including when the bundled source file or extractor version has changed. Restarting or refreshing the application does not explicitly reprocess completed samples.

To refresh completed results, use the [pipeline commands](development.md#pipeline-commands). An explicit run appends new processing runs and briefs. A failed attempt preserves the previously saved source, results, and decisions.

If automatic recovery fails, readable saved reviews and JSON downloads remain available with a recovery warning. Refresh to retry. Unavailable storage or an unreadable selected review receives a separate error message. See [recovery and consistent reads](architecture.md#recovery-and-consistent-reads) for the exact guarantees.

## JSON download

The download contains the selected persisted brief, its linked processing run, source metadata and evidence, and the complete ordered reviewer-decision history. It uses export schema `1.1.0`.

The source excerpt, displayed findings, reviewer controls, and download use the same saved result for that page load. If reprocessing finishes after the page reads its data, the next page rerun shows the newer result.

The export represents one brief and its linked records. It does not contain the full source text or a complete document-revision archive. See the [export contract](architecture.md#versioned-json-export) for compatibility and serialisation details.
