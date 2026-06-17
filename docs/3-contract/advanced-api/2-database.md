# The database API reads and writes the durable record

This file defines the public, in-process database API: the read seam an adjacent system relies on to read
everything a run recorded, and the write seam a specialized in-process producer uses to record into the durable
store. It is the second half of the advanced programmatic surface — `advanced-api/1-runners-and-clients.md` owns
running a pipeline; this file owns reading and writing the record those runs leave behind.

This is in-process `ai_pipeline_core` API. It is not the authoring surface (`api/` forbids application code from
touching the store — `api/9-integrations.md`), and it is not the out-of-process surface: a system that reads a run
over a boundary it did not author uses the externally readable read models in `runtime-api/`, which project these
same reads. It does not define the persistence backend, transport, span storage format, or how serialization is
performed; those are framework internals below this boundary.

## Clients import the database surface from the framework boundary

```python
from ai_pipeline_core import (
    CostTotals,
    DatabaseReader,
    DatabaseWriter,
    Document,
    DocumentEvent,
    LogRecord,
    RunState,
    RunSummary,
    SpanRecord,
)
```

### Constraints

- A client imports the database surface directly from `ai_pipeline_core`.
- A client does not import framework internals to read or write the record.
- The read seam is consumed by adjacent code, operating agents, sync and replay tooling, and the out-of-process
  read models; the write seam is consumed only by the framework and specialized in-process producers, never by
  application authoring code.

## The read seam exposes recorded runs

The read seam is the public, read-only surface for everything a run recorded: the runs themselves, the execution
spans, the documents, and the relationships between documents and the work that produced or consumed them. An
adjacent system projects this surface (for example, into the out-of-process read models in `runtime-api/`) and
relies on it being stable. It is read-only; nothing on it mutates a run.

A client reads a single run's authoritative state first-class — one summary, one delivered result, one terminal
status — without reconstructing it from span walks. The span and provenance methods are for descending into a run
once its current state is known, not for recovering that state.

### Reference

```text
class DatabaseReader:
    async def list_runs(self, *, labels: dict[str, str] | None = None) -> tuple[RunSummary, ...]
    async def get_run(self, run_id: str) -> RunSummary
    async def get_run_outputs(self, run_id: str) -> DocumentBundle | None
    async def get_run_cost(self, run_id: str) -> CostTotals
    async def get_run_spans(self, run_id: str) -> tuple[SpanRecord, ...]
    async def get_span(self, span_id: str) -> SpanRecord
    async def get_document(self, document_id: str) -> Document
    async def get_document_events(self, run_id: str) -> tuple[DocumentEvent, ...]
    async def get_document_producers(self, document_id: str) -> tuple[SpanRecord, ...]
    async def get_spans_referencing_document(self, document_id: str) -> tuple[SpanRecord, ...]
    async def get_run_logs(
        self, run_id: str, *, level: str | None = None, category: str | None = None
    ) -> tuple[LogRecord, ...]
    async def get_span_logs(
        self, span_id: str, *, level: str | None = None, category: str | None = None
    ) -> tuple[LogRecord, ...]
```

The records this surface returns expose the fields a client relies on:

- `RunSummary` — `run_id`, `execution_id` (the recorded execution attempt, disambiguating a reused `run_id`),
  `pipeline_name`, `state` (the run's observed `RunState`), `error` (the terminal message when the run failed,
  otherwise `None`), start and end timestamps, and total recorded cost. `state` is `RunState`, the one observed
  run-state vocabulary (`runtime-api/1-overview-and-state.md § The observed run-state is one shared type`); a raw
  summary read returns the recorded state, and `CRASHED` is established only by the reconcile read
  (`runtime-api/2-run-control.md`), so an unreconciled crashed run reads `RUNNING`. It also carries `labels`, the
  run's immutable correlation labels (`runtime-api/2-run-control.md § Correlation labels are immutable run
  metadata`) — an empty map when none were supplied at launch.
- `SpanRecord` — `span_id`, `run_id`, `kind` (the unit of work: pipeline, phase, task, judgment — one prompt
  execution — and the finer units beneath it), `status` (the per-unit status, including `cached` for a task served
  from cache and `skipped` for a unit gated around; this is a unit-level status, not a run-level `RunState`), timing,
  recorded cost, and `input_document_ids`/`output_document_ids` (wire: `input_doc_sha256s`/`output_doc_sha256s`), the
  identities of the documents the unit consumed and produced.
- `DocumentEvent` — the identity of a document and the recorded point in the run at which it became durable.
- `LogRecord` — one recorded log line attached to the unit that produced it: `span_id`, `run_id`, `timestamp`,
  `sequence_no` (its order within the unit), `level`, `category`, `message`, the structured `fields` recorded with
  the line, and the `exception` text when the line recorded one. This is the same line the out-of-process surface
  projects (`runtime-api/5-documents-and-logs.md`).
- `CostTotals` — aggregated recorded cost for a run or a subtree, returned by `get_run_cost`. It exposes `cost_usd`
  (the aggregated recorded dollar cost) and the token totals (input, output, cache-read, and reasoning) the framework
  recorded; cost is a first-class read field, not derived by the client.

The full internal structure of a span is a framework internal; this surface commits to the fields named here.

### Constraints

- `list_runs` returns one summary per recorded run, optionally filtered by `labels` (exact key/value equality — the
  one filter this seam applies directly); a client paginates or projects the result in its own memory. Server-side
  pagination and projection for an external reader are the out-of-process read models
  (`runtime-api/4-run-record-read-model.md`), not this in-process seam.
- `labels` are a run-level property: recorded on the run, returned on its `RunSummary`, filterable on `list_runs`
  by exact key/value equality, and inherited by the run's lifecycle events. They are not set per span — an
  individual unit's `SpanRecord` does not carry them — and label filtering never aggregates or groups
  (`4-limits-and-non-promises.md § Out of scope entirely`).
- A document carries no intrinsic labels. "Documents for a label" resolves through the producing run — the runs
  matching the label, then their recorded output documents — not from a field on the document
  (`runtime-api/5-documents-and-logs.md`).
- `get_run` returns the current summary of one run by `run_id`, including its observed `state` and error;
  `get_run_outputs` returns the delivered output bundle of a completed run, or `None` when the run has not
  completed. A client reads these before descending into spans.
- A client reads a run's state, error, and delivered outputs from `get_run`/`get_run_outputs`, not by inferring
  them from a span walk.
- `get_run_spans` returns every span recorded for one run, scoped by `run_id`; `get_span` returns one span by its
  own `span_id` without the parent `run_id`, the read a single-unit replay stands on (`tools/7-replay.md`).
- `get_document_producers` returns the spans that produced a document; `get_spans_referencing_document` returns the
  spans that consumed or cited it. Together they are the forward and reverse provenance a client uses to trace a
  conclusion to its evidence and to find everything affected by a document.
- `get_run_cost` returns the run's aggregated `CostTotals`; per-unit cost is read from each `SpanRecord`. A client
  reads recorded cost, it does not recompute it.
- `get_document` rehydrates a document by identity, the same call used for stored-document inputs in
  `advanced-api/1-runners-and-clients.md`.
- `get_run_logs` returns a run's recorded log lines and `get_span_logs` returns one unit's, each filterable by
  `level` and `category`. `get_run_logs` returns the run's one diagnostic account — the log lines of every
  cooperating part that touched the run, correlated to it — not only the application's own span logs
  (`3-guarantees.md § Durable, readable logs`). Logs are read through the seam, not scraped from process output; the
  tail-loss edge is named in `4-limits-and-non-promises.md`.
- Cost is read from the recorded `CostTotals` and from the recorded cost on each `SpanRecord`, not inferred from
  timing or recomputed by the client (`3-guarantees.md § Cost recording and attribution`).
- The read seam is read-only; a client does not mutate a run, a span, or a document through it.
- A client relies only on the record fields named here; deeper span structure is a framework internal and may not
  be relied on as contract.

### Anti-patterns

Wrong: a client answers "where did this conclusion come from?" by re-running the pipeline and reading logs.
Correction: walk `get_document_producers` and `get_spans_referencing_document` from the conclusion's document; the
record already holds the lineage.

Wrong: a client writes directly to the persistence layer to mark a run complete or patch a document. Correction:
runs change only by executing or resuming them through the `Runner`; the read seam never mutates.

### Examples

Read a run's authoritative state first, then descend into its spans only once that state is known:

```python
from ai_pipeline_core import DatabaseReader, RunState


async def summarize_run(reader: DatabaseReader, run_id: str) -> None:
    summary = await reader.get_run(run_id)
    if summary.state is RunState.FAILED:
        raise RuntimeError(f"Run {run_id} failed: {summary.error}")
    cost = await reader.get_run_cost(run_id)
    spans = await reader.get_run_spans(run_id)
    print(f"{summary.pipeline_name}: {len(spans)} units, ${cost.cost_usd:.2f}")
```

Trace a delivered conclusion back to its evidence by walking provenance from its document:

```python
from ai_pipeline_core import DatabaseReader


async def trace_to_evidence(reader: DatabaseReader, conclusion_document_id: str) -> tuple[str, ...]:
    producers = await reader.get_document_producers(conclusion_document_id)
    evidence_ids: list[str] = []
    for span in producers:
        evidence_ids.extend(span.input_document_ids)
    return tuple(evidence_ids)
```

Read a completed run's delivered outputs and its unified diagnostic account (the logs of every cooperating part,
correlated to the run), filtered to errors:

```python
from ai_pipeline_core import DatabaseReader


async def read_outputs_and_errors(reader: DatabaseReader, run_id: str) -> None:
    outputs = await reader.get_run_outputs(run_id)  # None until the run completes
    errors = await reader.get_run_logs(run_id, level="ERROR")  # spans all cooperating parts, not only app spans
    print(f"delivered: {outputs is not None}; error lines: {len(errors)}")
```

Rehydrate a document by identity and read one unit's logs:

```python
from ai_pipeline_core import DatabaseReader


async def read_unit(reader: DatabaseReader, document_id: str, span_id: str) -> None:
    document = await reader.get_document(document_id)  # same call used for stored-document inputs
    span_logs = await reader.get_span_logs(span_id, category="integration")
    print(f"{document.name}: {len(span_logs)} integration log lines")
```

Read the recorded corpus to compare a baseline run against a candidate — the substrate an agent uses for
benchmarking; the comparison and the judgment of which is better are the agent's, not the framework's
(`3-guarantees.md § Preserved, traceable, replayable runs`):

```python
from ai_pipeline_core import DatabaseReader


async def compare_runs(reader: DatabaseReader, baseline_run_id: str, candidate_run_id: str) -> tuple[float, float]:
    baseline = await reader.get_run_cost(baseline_run_id)
    candidate = await reader.get_run_cost(candidate_run_id)
    # The framework supplies recorded inputs, outputs, and cost; scoring which run is better is the consumer's.
    return baseline.cost_usd, candidate.cost_usd
```

## The write seam records into the durable store

The write seam is the public, append-oriented surface a specialized in-process producer uses to record runs,
documents, content, and logs into the durable store. It exists for the framework's own runtime and for
framework-adjacent producers that stand up or record a run — not for application tasks, which never touch the store
(`api/9-integrations.md`). The runner owns recording for an ordinary run; a producer reaches the write seam only
when it is recording on the framework's behalf.

The store is append-only as a whole, not only for spans: every write adds durable history, and nothing a producer
writes replaces or deletes what was already recorded. `update_document_summary` and any other `update`-named call
records a new version or projection beside the prior record rather than mutating stored bytes in place. Old history
may be archived or compressed, but the recorded facts are never discarded (`3-guarantees.md § Durable record`).
This invariant holds identically across the configured backends; which backend is in use is a configuration
concern below this boundary (`5-configuration.md`).

### Reference

```text
class DatabaseWriter:
    @property
    def supports_remote(self) -> bool

    async def insert_span(self, span: SpanRecord) -> None
    async def save_document(self, document: Document) -> None
    async def save_document_batch(self, documents: tuple[Document, ...]) -> None
    async def save_blob(self, content: bytes) -> None
    async def save_blob_batch(self, contents: tuple[bytes, ...]) -> None
    async def save_logs_batch(self, logs: tuple[LogRecord, ...]) -> None
    async def update_document_summary(self, document_id: str, summary: str) -> None
    async def flush(self) -> None
    async def shutdown(self) -> None
```

- `insert_span` appends one span version recording one unit of work; spans are append-only versions, never patched
  in place.
- `save_document` and `save_document_batch` persist a document, including its primary content blob; `save_blob` and
  `save_blob_batch` persist additional content bytes a document references, such as attachment content. The blob key
  is the content hash the framework derives from the bytes — the producer passes content, not an identity, consistent
  with never minting identity by hand.
- `save_logs_batch` persists execution log lines attached to the unit that produced them.
- `update_document_summary` records a human-readable summary against an already-persisted document as a new version
  beside it; it adds the summary additively and does not mutate, replace, or delete the document's content or
  identity.
- `flush` drains buffered writes to the durable store; `shutdown` closes the backend.
- `supports_remote` reports whether the configured backend can back cross-process and distributed execution — the
  capability the framework reads to decide whether a child pipeline runs in-process or as a separately-deployed run
  (`runtime-api/6-child-pipelines.md`).

### Constraints

- The write seam is used by the framework runtime and by specialized in-process producers, never by application
  tasks, phases, pipelines, prompt contracts, or tools.
- Writes are append- and record-oriented: a producer records what happened. It does not rewrite a completed run
  into a state the run did not reach, and it does not fabricate a terminal status the execution never recorded.
- A producer records immediate provenance and identity as the framework computes them; it does not mint document
  identity or construct provenance by hand (`api/2-documents.md`).
- The authoritative account of a run is what was recorded; reconciling a crashed or interrupted run to that record
  is `runtime-api/2-run-control.md`, not an invitation to overwrite recorded spans.
- Which backend the reader and writer bind to is a configuration concern (`5-configuration.md`); the same API
  behaves identically across backends.

### Examples

A specialized producer recording one document and its content, then flushing — recording on the framework's behalf,
never from application task code:

```python
from ai_pipeline_core import DatabaseWriter, Document


async def record_imported_document(writer: DatabaseWriter, document: Document) -> None:
    for attachment in document.attachments:
        await writer.save_blob(attachment.content)  # attachment bytes
    await writer.save_document(document)  # persists the document and its primary content
    await writer.flush()
```
