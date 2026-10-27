# Tender-to-Inspection Brief — Product Brief

## Purpose

Tender-to-Inspection Brief helps technical reviewers turn public construction tender excerpts into structured, source-traced review briefs.

The application provides a transparent first-pass review structure by identifying technical scopes, surfacing information gaps, suggesting follow-up questions, and linking detected domains to source evidence. It supports human technical review and does not make legal, regulatory, compliance, safety, or engineering decisions.

## Users and review need

Primary users are technical reviewers, inspection coordinators, and specialists preparing for an early tender, document, or design review.

They need to:

- understand the declared project scope;
- identify technical domains requiring attention;
- inspect supporting source evidence;
- highlight missing or unclear information;
- prepare focused follow-up questions;
- record and revisit decisions on persisted generated findings.

## Current workflow

The application accepts bundled public or synthetic excerpts and short public, non-confidential excerpts pasted into the Streamlit interface. Bundled results are persisted; pasted excerpts remain session-only.

Each generated brief contains a summary, technical scopes and review focus areas, information gaps, suggested questions, source-labelled evidence, fixed `low` baseline confidence, and a human-review-required marker.

For persisted bundled briefs, reviewers can record `accepted`, `rejected`, or `needs_follow_up` decisions with optional notes. Every saved change appends a history event without modifying the generated brief.

Persisted briefs can be inspected in a read-only JSON preview and downloaded as deterministic, versioned JSON containing document metadata, the linked processing run, the generated brief and source evidence, and the complete ordered reviewer-decision history.

Saved decisions remain tied to the brief the reviewer inspected. Refreshing a result creates a new brief without transferring earlier decisions. Failed processing preserves the previous saved review.

See the [user guide](user-guide.md) for the review workflow and [architecture and data contracts](architecture.md) for persistence, recovery, and export guarantees. Sample sources and evaluation boundaries are documented in [data and validation](data-and-validation.md).

## Boundaries

The application does not currently provide:

- complete tender-dossier ingestion or document management;
- BIM coordination, site-inspection records, or defect tracking;
- regulatory or compliance assessment;
- autonomous engineering decisions;
- persistent storage or reviewer decisions for pasted excerpts;
- reviewer identity, authentication, or simultaneous multi-user editing;
- general multilingual or semantic document understanding.

## Direction

Near-term priorities are:

- broader qualitative validation;
- a documented ingestion path for tender documents and PDFs;
- more capable extraction methods only when they can be evaluated against the transparent baseline.
