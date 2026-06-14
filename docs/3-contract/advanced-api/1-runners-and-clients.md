# Runners and clients drive a compiled pipeline

This file defines the public contract for executing a compiled pipeline and reading back what a run produced. It
names the typed run API, run identity, resume and partial-range execution, single-unit execution with full
recording, stored-document inputs and rehydration, and cache control. The database API an adjacent system relies on
to read and write the record is owned by `advanced-api/2-database.md`. This file does not define the persistence
backend, transport, span storage format, scheduling, or the internal execution engine; those are framework
internals below this boundary.

This is the advanced programmatic surface: the runner for code that drives a run, paired with the database API in
`advanced-api/2-database.md` for code that reads and writes its record. This file is the runner half only — **all
record reads and writes (runs, spans, documents, provenance, logs, cost) live in `advanced-api/2-database.md`**, and
this file points there for them. It is separate from the authoring `api/` subtree (files 1–9), which is the surface
an agent uses to *write* a pipeline package. The audience for this file is the code that *drives* a pipeline — a
client process, an operating agent, or an adjacent system — not the application author declaring the pipeline
package. It is also the programmatic substrate an application's tests stand on; how tests are authored against it is
documented in `testing/` (`testing/2-writing-tests.md`). Authoring is covered in `api/1-application.md` through
`api/9-integrations.md`; this file is how something runs what those declare. Deployment — packaging and publishing
an application — is an operator capability documented in the tools surface (`6-tools-and-the-development-loop.md`),
not part of this programmatic driving contract.

## Clients import run-surface names

A client imports the run surface from the framework boundary. The pipeline instance comes from the application
package; everything needed to run it, resume it, exercise one unit of it, or read its record comes from
`ai_pipeline_core`.

### Reference

```python
from ai_pipeline_core import (
    Attachment,
    CachePolicy,
    ContractRunResult,
    DatabaseReader,
    Document,
    PromptResult,
    RunResult,
    RunStatus,
    Runner,
    TerminalError,
)
```

### Constraints

- A client imports the run surface directly from `ai_pipeline_core`.
- A client imports the pipeline instance from the application package and does not reconstruct it.
- A client does not import framework internals to run, resume, or inspect a pipeline.

## A run takes an input bundle and returns an output bundle

`Runner.run` executes one compiled pipeline against one root input bundle and returns the pipeline's declared output
bundle. The runner owns persistence, span recording, retries, and recovery state; the client supplies inputs, run
configuration, and an optional run identity.

### Reference

```text
class Runner:
    async def run[OutputsT: DocumentBundle](
        self,
        pipeline: Pipeline[Any, Any, OutputsT],
        *,
        inputs: DocumentBundle,
        run_config: RunConfig,
        run_id: str | None = None,
        cache: CachePolicy = CachePolicy.REUSE,
    ) -> RunResult[OutputsT]
```

`RunResult[ResultT]` is the recorded result of executing a unit, where `ResultT` is
`Document | tuple[Document, ...] | DocumentBundle` — a whole pipeline's delivered output bundle, or the single
document or tuple a single task returns. It exposes:

- `run_id: str` — the identity of this run.
- `status: RunStatus` — `COMPLETED`, `FAILED`, or `INCOMPLETE`.
- `outputs: ResultT | None` — the delivered result when `status` is `COMPLETED`, otherwise `None`.
- `error: str | None` — the terminal failure message when `status` is `FAILED`, otherwise `None`.

`Runner.run` binds `ResultT` to the pipeline's declared output bundle (`RunResult[OutputsT]` with
`OutputsT: DocumentBundle`); `run_task` binds it to the task's declared return.

`RunStatus` here is the result of a blocking in-process call: `Runner.run` returns once the run has completed,
failed, or stopped incomplete, so it reports `COMPLETED`, `FAILED`, or `INCOMPLETE`. It overlaps the broader
observed run-state `RunState` on `COMPLETED` and `FAILED`; `INCOMPLETE` is the blocking-call name for a run that
returned before reaching a terminal `RunState` and has no `RunState` member of its own. The run's observed state —
the `RunState` members the blocking call never returns, `PENDING`, `RUNNING`, `CANCELLED`, and `CRASHED` — is read
through the read seam (`get_run`, `advanced-api/2-database.md`) and the runtime surface; `CRASHED` specifically is
established only by the reconcile read (`runtime-api/2-run-control.md`). `RunState` is defined once in
`runtime-api/1-overview-and-state.md § The observed run-state is one shared type`.

### Examples

```python
from ai_pipeline_core import AIModelRef, RunStatus, Runner, TerminalError

from review_app import pipeline
from review_app.documents import ReviewInputs
from review_app.run_config import ReviewRunConfig


async def run_review(runner: Runner, inputs: ReviewInputs, model_ref: AIModelRef) -> None:
    result = await runner.run(
        pipeline,
        inputs=inputs,
        run_config=ReviewRunConfig(assessment_model=model_ref),
    )
    if result.status is not RunStatus.COMPLETED:
        raise TerminalError(f"Review run {result.run_id} did not complete: {result.error}")
    assessment = result.outputs.assessment
```

### Constraints

- The `inputs` bundle is the root input bundle type declared by the pipeline class.
- The `run_config` value is the run configuration type declared by the pipeline class.
- A run that cannot validate its declared shape fails before any task executes; the framework does not start a run
  it has already proved cannot complete.
- A client reads delivered documents from `result.outputs`, not from the persistence layer directly.
- A client does not interpret `RunStatus` by string comparison on internal fields; it branches on the enum.
- A successful run returns the declared output bundle; a client does not reconstruct outputs by querying spans.

### Constructing a runner

A `Runner` is constructed with no arguments and binds to the persistence store selected by environment
configuration (`5-configuration.md`). Which store backs it is a configuration concern, not a property the client
inspects, and the same `Runner` API behaves identically whether it is backed by local storage on one machine or
shared storage across many.

```text
class Runner:
    def __init__(self) -> None: ...
```

```python
from ai_pipeline_core import Runner

runner = Runner()
```

### Constraints

- A client constructs one `Runner` and reuses it across runs; it does not construct a runner per call to share
  state through process memory.
- A client treats the store handle as opaque and does not read or write persisted records around the runner.

### Local execution needs no infrastructure

A `Runner()` constructed with no environment configuration binds to local filesystem storage and in-process
execution: no external orchestrator, store, or event bus is required. The same `Runner.run(...)` call produces the
same documents, the same spans, the same provenance, and the same inspectable, replayable record it produces against
a distributed backend — only the placement and scale differ. This is the in-process face of *operational parity*
(`3-guarantees.md § Operational parity across placements`); moving to production is the configuration change in
`5-configuration.md`, not a different API.

```python
from ai_pipeline_core import Runner

# No configuration: filesystem-backed, in-process, zero external services.
runner = Runner()
```

## Driver code constructs root documents at the run boundary

Root documents are created by the code that drives a run, not by application-authored tasks or any other authoring
code (`api/2-documents.md § Root documents are created at the run boundary`). A driver mints each typed root document
from raw external material — text, bytes, or a fetched source — through the root-construction classmethod
`create_root`, then assembles them into the pipeline's declared input bundle. `create_root` is the in-process
counterpart of the out-of-process boundary that resolves raw inputs into typed root documents
(`runtime-api/2-run-control.md § Root inputs enter as raw external material`); the framework owns the resulting
identity and root provenance.

### Reference

```text
classmethod create_root(
    *,
    content: T,
    name: str | None = None,
    attachments: tuple[Attachment, ...] = (),
) -> Self
```

### Examples

```python
from ai_pipeline_core import Runner

from review_app.documents import (
    ReviewEvidence,
    ReviewEvidenceDocument,
    ReviewInputs,
    ReviewRequest,
    ReviewRequestDocument,
)
from review_app.run_config import ReviewRunConfig


async def run_from_raw_inputs(runner: Runner, model_ref, raw_request: str, raw_evidence: list[str]) -> None:
    request = ReviewRequestDocument.create_root(
        content=ReviewRequest(company_name="Acme Corp", review_focus="vendor risk"),
        name="request",
    )
    evidence = tuple(
        ReviewEvidenceDocument.create_root(content=ReviewEvidence(title=f"evidence-{i}", body=body))
        for i, body in enumerate(raw_evidence)
    )
    await runner.run(
        pipeline,  # imported from the application package
        inputs=ReviewInputs(request=request, evidence=evidence),
        run_config=ReviewRunConfig(assessment_model=model_ref),
    )
```

### Constraints

- `create_root` is a driver-and-runtime capability, used by code that drives a run. Application authoring code —
  tasks, phases, pipelines, prompt contracts, tools — never calls it; a task that finds it needs a root document has
  a design error (`api/2-documents.md`).
- The framework assigns root identity and root provenance; the driver does not mint identity or pass provenance
  sources to `create_root`. Non-root documents are created by tasks through the provenance-recording factories in
  `api/2-documents.md`, not through `create_root`.
- A root document constructed this way enters a run only through the pipeline's declared input bundle.
- `create_root` takes `content`, `name`, and `attachments` only. The read-model `description` and `summary` are not
  in-process root-construction parameters — they are framework-owned read-model metadata
  (`runtime-api/5-documents-and-logs.md`): an out-of-process driver may supply a `description`/`summary` label at the
  run boundary (`runtime-api/2-run-control.md`), and the framework may attach a generated summary later through the
  write seam (`advanced-api/2-database.md`), but an in-process driver does not set them at construction. This is the
  deliberate asymmetry between the two boundaries, not a missing parameter.

## Run identity is framework-assigned and stable

A run has one stable identity. When the client does not supply `run_id`, the framework assigns one; when the client
supplies one, the framework uses it. The identity is the correlation key for resume, recording, inspection, and the
read seam.

### Constraints

- `run_id` is an opaque stable string; a client does not parse structure out of it or construct identity from
  business values.
- A client supplies `run_id` only to adopt an identity it already owns (for example, to correlate a run with an
  external request); otherwise it lets the framework assign one.
- Reusing a `run_id` resumes or extends that run's record; it does not silently start a second independent run
  under the same identity.

## Resume continues an interrupted run

`Runner.resume` continues a run that stopped before completion. The runner re-drives the deterministic plan against
the recorded task results, so work already recorded as complete is not recomputed; execution continues from the
first incomplete phase.

### Reference

```text
async def resume[OutputsT: DocumentBundle](
    self,
    run_id: str,
    *,
    cache: CachePolicy = CachePolicy.REUSE,
) -> RunResult[OutputsT]
```

### Constraints

- `resume` takes the `run_id` of an existing run and continues it; it does not accept a new input bundle.
- Resume does not change the inputs, run configuration, or plan recorded for the run; a different input bundle or
  configuration is a different run.
- Completed tasks recorded for the run are reused; resume recomputes only work that did not complete.
- Resuming a run that already completed returns its recorded `RunResult` without re-executing it.

### Anti-patterns

Wrong: a client catches an interrupted run, builds a fresh input bundle, and starts a new run to "continue."
Correction: call `resume(run_id)`; the framework owns what was already done.

## Partial-range execution runs a contiguous span of phases

A client may execute a contiguous range of a pipeline's declared phases rather than the whole pipeline. Partial
runs exist for diagnosis and iteration: re-execute one stage against recorded upstream state without paying for the
phases before it. The range is named by phase class, and the phases before the range are satisfied from recorded
documents.

### Reference

```text
async def run_range[OutputsT: DocumentBundle](
    self,
    pipeline: Pipeline[Any, Any, OutputsT],
    *,
    run_id: str,
    start_after: type[Phase] | None = None,
    stop_after: type[Phase] | None = None,
    cache: CachePolicy = CachePolicy.REUSE,
) -> RunResult[OutputsT]
```

### Constraints

- `run_range` runs against an existing `run_id` whose upstream phase outputs are already recorded.
- `start_after` and `stop_after` name phase classes declared by the pipeline; the executed range is the contiguous
  phases between them.
- Documents required by the range and produced by earlier phases are read from the run record, not recomputed.
- A range whose required upstream documents are not present in the run record fails before executing; the framework
  does not fabricate missing upstream state.
- Partial-range execution does not invent phases, routes, or delivery fields beyond the compiled plan.

## Single units run with full recording

A client runs one task or one prompt contract in isolation under the runner, with the same recording a full run
produces. This is how an agent exercises a unit during development and how application tests assert behavior: the
unit executes, its spans and documents are recorded, and the result is inspectable and replayable exactly as it is
inside a full run.

### Reference

```text
async def run_task[OutputT: Document | tuple[Document, ...] | DocumentBundle](
    self,
    task: Task,
    *,
    inputs: Mapping[str, Document | tuple[Document, ...]],
    run_id: str | None = None,
    cache: CachePolicy = CachePolicy.REUSE,
) -> RunResult[OutputT]

async def run_contract[OutputT: FrozenBaseModel](
    self,
    contract: PromptContract[OutputT],
    *,
    model: AIModelRef,
    run_id: str | None = None,
) -> ContractRunResult[OutputT]
```

`ContractRunResult[OutputT]` is the recorded result of a single contract run, parallel to `RunResult` for tasks:

- `run_id: str` — the identity of this contract run.
- `status: RunStatus` — `COMPLETED`, `FAILED`, or `INCOMPLETE`.
- `result: PromptResult[OutputT] | None` — the prompt result when `status` is `COMPLETED`, otherwise `None`.
- `error: str | None` — the terminal failure message when `status` is `FAILED`, otherwise `None`.

### Examples

```python
from ai_pipeline_core import AIModelRef, RunStatus, Runner

from review_app.documents import ReviewEvidenceDocument, ReviewRequestDocument
from review_app.tasks.assess_risk import AssessRiskTask


async def exercise_assess_risk(
    runner: Runner,
    request: ReviewRequestDocument,
    evidence: tuple[ReviewEvidenceDocument, ...],
    model_ref: AIModelRef,
) -> None:
    result = await runner.run_task(
        AssessRiskTask(model=model_ref),
        inputs={"request": request, "evidence": evidence},
    )
    assert result.status is RunStatus.COMPLETED
```

```python
from ai_pipeline_core import AIModelRef, RunStatus, Runner

from review_app.documents import ReviewEvidenceDocument
from review_app.prompt_contracts import AssessEvidenceContract


async def exercise_assess_contract(
    runner: Runner,
    evidence: ReviewEvidenceDocument,
    model_ref: AIModelRef,
) -> None:
    outcome = await runner.run_contract(
        AssessEvidenceContract(evidence=evidence),
        model=model_ref,
    )
    assert outcome.status is RunStatus.COMPLETED
    assessment = outcome.result.response
```

### Constraints

- `run_task` schedules one task instance with resolved document inputs keyed by the task's `run` parameter names.
- `run_contract` executes one prompt contract instance with full recording; its `ContractRunResult` carries the
  run identity and status alongside the `PromptResult` the contract returns inside a task.
- A single-unit run carries its own `run_id`; the recorded run is fetched afterward through the read seam
  (`get_run`, `advanced-api/2-database.md`), and replayed through the tools surface.
- A single-unit run records the same spans and documents a full run records; its result is inspectable and
  replayable through the read seam and the tools surface.
- Single-unit execution does not bypass validation, retry, or recording to run faster.
- A client does not reach into framework internals to run a unit without recording.

### Anti-patterns

Wrong: a test calls a task's `run` method directly to avoid the runner, so the execution leaves no record and
cannot be inspected or replayed when it fails. Correction: run the task through `runner.run_task`, which records it
exactly as a full run does.

## Stored documents enter a run by identity

A run input may be a document produced by an earlier run. The client rehydrates that document by its identity and
places it in the input bundle. The rehydrated document keeps its original identity, so provenance and citations
that reference it remain valid across runs.

### Reference

```text
class DatabaseReader:
    async def get_document(self, document_id: str) -> Document
```

This shows only the rehydration method `DatabaseReader` is used for here; its full read surface is in
`advanced-api/2-database.md § The read seam exposes recorded runs`.

### Examples

```python
from ai_pipeline_core import AIModelRef, DatabaseReader, RunResult, Runner

from review_app import pipeline
from review_app.documents import ReviewEvidenceDocument, ReviewInputs, ReviewOutputs
from review_app.run_config import ReviewRunConfig


async def rerun_with_prior_request(
    runner: Runner,
    reader: DatabaseReader,
    prior_request_id: str,
    evidence: tuple[ReviewEvidenceDocument, ...],
    model_ref: AIModelRef,
) -> RunResult[ReviewOutputs]:
    request = await reader.get_document(prior_request_id)
    return await runner.run(
        pipeline,
        inputs=ReviewInputs(request=request, evidence=evidence),
        run_config=ReviewRunConfig(assessment_model=model_ref),
    )
```

### Constraints

- A document rehydrated by identity is the same durable document; its identity, content, and metadata are
  unchanged across runs.
- A client supplies a rehydrated document as a root input through the pipeline's input bundle, the same way a
  freshly constructed root document enters.
- A client does not copy a stored document's content into a new document to "import" it; rehydration preserves
  identity, copying breaks it.
- A document identity from one run resolves to the same document in another run; identity is not run-scoped.

### Anti-patterns

Wrong: a client reads a prior run's output text and constructs a new root document from it, so cross-run provenance
and citations no longer resolve. Correction: rehydrate the document by identity so references to it stay valid.

## Cache control selects reuse, refresh, or bypass

Task results are cached by content. A client chooses how a run treats that cache through `CachePolicy`, and the run
record reports which units were cache hits.

### Reference

`CachePolicy` is a closed set:

- `REUSE` — reuse a cached task result when the cache key matches; compute and store otherwise. This is the
  default.
- `REFRESH` — recompute every task and overwrite cached results, even when a cached result exists.
- `BYPASS` — recompute every task and do not read or write the cache for this run.

### Constraints

- `cache` is one of the `CachePolicy` values; a client does not pass a raw boolean or string.
- Cache reuse is content-addressed; a cached result is reused only when the task's inputs and configuration produce
  the same cache key.
- A client uses `REFRESH` to force recomputation while keeping the cache populated, and `BYPASS` to recompute
  without disturbing the cache.
- Cache hit visibility is read from the run record: a reused task is recorded with cached status, a recomputed task
  with completed status (see `advanced-api/2-database.md § The read seam exposes recorded runs`).
- A client does not infer cache behavior from timing; it reads recorded status.

## The read seam is owned by the database API

Reading back what a run recorded — the runs themselves, the execution spans, the documents, the provenance
relationships, the logs, and the recorded cost — is the read seam, and it is owned by the database API in its own
file, `advanced-api/2-database.md`. That file defines `DatabaseReader` (the read-only surface this runner stands
on) alongside `DatabaseWriter` (the write seam specialized in-process producers use). The runner and the database
API are one programmatic surface split across two files: this file runs a pipeline; `2-database.md` reads and
writes the record it leaves behind.

A client reads a single run's authoritative state first-class — one summary, one delivered result, one terminal
status — through `get_run` and `get_run_outputs`, then descends into spans and provenance once that state is known.
`get_document` rehydrates a document by identity, the same call used for stored-document inputs above. The full
reference, record fields (`RunSummary`, `SpanRecord`, `DocumentEvent`, `LogRecord`, `CostTotals`), and constraints
are in `advanced-api/2-database.md § The read seam exposes recorded runs`. An external system that reads a run over
a boundary it did not author uses the out-of-process read models in `runtime-api/4-run-record-read-model.md`, which
project the same reads.
