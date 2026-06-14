# Reading documents and logs from outside

This file defines the externally readable read models for a run's documents — their metadata, content, provenance
graph, and event history — and its execution logs. Like the run-record read models in
`runtime-api/4-run-record-read-model.md`, these are the out-of-process projection of the in-process read seam in
`advanced-api/2-database.md`, served to a reader that cannot import the framework's definitions.

## Reading documents

### Capabilities

- **Get a document's metadata.** By document identity.
- **Get a document's content.** The raw bytes of a document, and of each attachment that companions it.
- **Batch document metadata.** Metadata for many documents at once, for a consumer assembling a view.
- **Get a document's provenance graph.** The forward and reverse provenance around a document — what produced it and
  what consumed or cited it — assembled into a graph a consumer walks to trace a conclusion to its evidence.
- **Get a document's event history.** When a document became durable, across the runs that produced or referenced
  it; the record-derived companion to the live document events in `runtime-api/3-observation-and-notifications.md`.

### Reference

The document read model:

```text
document = {
    "document_id": str,             # wire field: document_sha256; the framework-owned identity
    "content_id": str,              # wire field: content_sha256; the content (blob) identity
    "document_type": str,           # wire field: class_name; the document class name
    "name": str,
    "description": str | None,      # framework-owned read-model metadata, may be absent (see below)
    "summary": str | None,          # framework-owned read-model metadata, may be absent (see below)
    "media_type": str,              # wire field: mime_type
    "size_bytes": int,              # wire field: size
    "content_sources": [str, ...],  # wire field: derived_from; immediate content provenance (upstream document ids)
    "causal_sources": [str, ...],   # wire field: triggered_by;  immediate causal provenance (upstream document ids)
    "attachments": [
        {"name": str, "description": str | None,
         "content_id": str,         # wire field: sha256
         "media_type": str,         # wire field: mime_type
         "size_bytes": int},        # wire field: size
        ...
    ],
}
```

`content_sources` and `causal_sources` carry immediate upstream documents only, never a copied-forward ancestry —
the same 0.3.0 names the authoring contract uses (`api/2-documents.md § Factories record immediate provenance`),
with their wire spellings (`derived_from`, `triggered_by`) annotated alongside. The provenance-graph read model
carries the document nodes, the links between them, the producing unit for each node, and the run each belongs to,
with external-source references included on request.

### What is surfaced beyond the delivered outputs

There is no per-document visibility marker. Authorization is at the run boundary: a consumer authorized to read a
run can read every document that run recorded. The delivered output bundle is the curated top-level result; any
other recorded document — an intermediate, an input, a degraded outcome — is readable by the same authorized
consumer by its identity or by walking to it through the provenance graph. The framework maintains no boolean and no
document-type rule that gates each document's exposure; the only boundary is whether the consumer may read the run
at all (`runtime-api/1-overview-and-state.md`, `4-limits-and-non-promises.md § Out of scope entirely`).

### `description` and `summary` are read-model projection metadata

`description` and `summary` on the document read model are **read-model projection metadata** — fields the framework
may attach to a document for an outside reader. They are deliberately distinct from the two layers an author works
in: they are not part of the authored document **wrapper** (which carries `name` only, `api/2-documents.md`), and
they are not the same thing as the descriptive prose an author puts in a document's typed **content model**. The
framework may populate them from a label a driver supplied for a root input at the run boundary
(`runtime-api/2-run-control.md`) or from a framework-generated summary recorded against an already-persisted
document through the write seam (`advanced-api/2-database.md`). A consumer treats them as a best-effort projection
for display: either may be absent, an author cannot set them, and neither is guaranteed to equal any field of the
document's content. A consumer that needs an author-meant value reads the typed content, not these projections.

### Constraints

- There is no `publicly_visible` field on the document read model; exposure is governed by run-boundary
  authorization, not by a per-document marker or a per-type rule. Within an authorized run, every recorded document
  is readable by identity or provenance walk.
- `description` and `summary` are framework-owned read-model metadata, not authoring-wrapper fields; an author does
  not set them (`api/2-documents.md`).
- Document identity, content, and provenance are unchanged across runs; a document read from outside is the same
  durable document the in-process read seam returns (`advanced-api/2-database.md`).
- A document's content is read in its recorded serialized form — a `Document[str]` as text, a
  `Document[FrozenBaseModel]` as its JSON serialization, a `Document[bytes]` as bytes — carrying its `document_type`
  (the class name) and `media_type`. The framework publishes typed JSON Schemas for the run's root inputs
  (`document_input_schema`) and its delivered result (`result_schema`) on the launch descriptor
  (`runtime-api/2-run-control.md`); it does not maintain a per-type content-schema registry for every intermediate
  document type. An external reader decodes an intermediate document from its self-describing serialized form and its
  `document_type`, not from a published schema for that type.
- The read model commits to the fields named here; deeper internal document structure is a
  framework internal.

### Endpoint and example

The supported runtime exposes document reads as `GET /documents/{document_id}` (metadata),
`GET /documents/{document_id}/content` (raw bytes), `GET /documents/{document_id}/graph` (provenance graph),
`POST /documents/batch` (metadata for many documents at once), and `GET /documents/{document_id}/events` (event
history). The boundary carries the wire keys (the 0.3.0 name annotated in the reference block above):

```text
GET /documents/7QF3K2ABCD

200 →
{
    "document_sha256": "7QF3K2ABCD",      # 0.3.0 name: document_id
    "content_sha256": "C9X1...",          # 0.3.0 name: content_id
    "class_name": "RiskAssessmentDocument",  # 0.3.0 name: document_type
    "name": "risk-assessment",
    "description": null,
    "summary": "Medium risk; one unresolved director-list discrepancy.",
    "mime_type": "application/json",      # 0.3.0 name: media_type
    "size": 2048,                         # 0.3.0 name: size_bytes
    "derived_from": ["AB12...", "CD34..."],  # 0.3.0 name: content_sources
    "triggered_by": ["EF56..."],          # 0.3.0 name: causal_sources
    "attachments": []
}
```

Batch metadata takes a list of document identities and returns one document shape per identity, for a consumer
assembling a view without one round trip per document:

```text
POST /documents/batch
{"document_ids": ["7QF3K2ABCD", "AB12...", "CD34..."]}

200 →
{"documents": [ {"document_sha256": "7QF3K2ABCD", ...}, {"document_sha256": "AB12...", ...},
                {"document_sha256": "CD34...", ...} ]}
```

Event history reports when a document became durable, across the runs that produced or referenced it — the
record-derived companion to the live document events in `runtime-api/3-observation-and-notifications.md`, carrying
the same identities so a consumer merges the two without duplication:

```text
GET /documents/7QF3K2ABCD/events

200 →
{"events": [ {"document_sha256": "7QF3K2ABCD", "run_id": "review-2026-06-14",
              "span_id": "9KQ2-assess-risk", "timestamp": "2026-06-14T12:30:55Z"} ]}
```

## Reading logs

A run is readable from outside as one diagnostic account. The log lines of every cooperating part that touched the
run — the machine, the executing and scheduling machinery, the operating surface, the application's own steps, and
the external services it called — are gathered into one run-correlated account, durably readable by run and by unit,
filterable by level and category (`3-guarantees.md § Durable, readable logs`). A consumer reads that whole account
in one place rather than reaching a separate, differently-shaped log store per subsystem. This is the out-of-process
projection of the `get_run_logs` and `get_span_logs` reads in `advanced-api/2-database.md`.

### Capabilities

- **Get a run's logs.** All recorded log lines for a run, filterable by level and by category, bounded in size, with
  optional fan-out into the run's child pipelines when reading a top-level run.
- **Get a unit's logs.** The recorded log lines for one unit of work, filterable by level and by category.

### Reference

```text
log_line = {
    "run_id": str,
    "span_id": str,            # the unit the line attaches to
    "timestamp": str,
    "sequence": int,           # wire field: sequence_no; order within the unit
    "level": str,              # DEBUG | INFO | WARNING | ERROR
    "category": str,
    "message": str,
    "fields": {...},           # wire field: fields_json; structured fields recorded with the line
    "exception": str | None,   # wire field: exception_text; exception text when the line recorded one
}
```

This is the same recorded log line as the in-process `LogRecord` (`advanced-api/2-database.md`), projected over the
boundary; the in-process and out-of-process surfaces describe the same line.

### Endpoint and example

The supported runtime exposes log reads as `GET /runs/{run_id}/logs` and `GET /spans/{span_id}/logs`, filterable by
level and category and bounded in size:

```text
GET /runs/review-2026-06-14/logs?level=ERROR

200 →
{
    "logs": [
        {"run_id": "review-2026-06-14", "span_id": "9KQ2-assess-risk", "timestamp": "2026-06-14T12:30:54Z",
         "sequence": 7, "level": "ERROR", "category": "integration",
         "message": "registry lookup timed out; retrying", "fields": {"attempt": 1}, "exception": null}
    ]
}
```

### Constraints

- Logs are read through this surface, not scraped from process output; they are durable diagnostic records, part of
  the supported record (`3-guarantees.md § Durable, readable logs`).
- A consumer filters by level and category server-side and reads logs in bounded portions rather than as one
  undivided dump.
- The durable record, not the last buffered log line, is authoritative: a hard process kill can drop the very tail
  of the live log stream (`4-limits-and-non-promises.md § Zero-loss logging`).
