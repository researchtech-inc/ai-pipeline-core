# Tasks turn resolved documents into durable results

Tasks are application-authored execution boundaries inside a compiled pipeline. A task receives resolved documents,
performs one coherent unit of Python work, and returns durable document state while the framework owns persistence,
retries, prompt execution mechanics, and service values. This file names the public task contract; phase planning,
prompt contracts, document factories, and integration adapters are outside this task contract.

## Task modules import execution-boundary names

Task modules import the boundary names needed to define executable work. Local concurrency for prompt calls uses
standard `asyncio` inside the task body; it is not a framework helper surface.

### Reference

```python
from ai_pipeline_core import (
    AIModelRef,
    Document,
    DocumentBundle,
    PromptResult,
    RetryableError,
    Task,
    TerminalError,
)
```

## Task is the execution boundary

The task boundary surrounds application-authored Python work. One task produces one coherent durable contribution.
Work that needs a separate retry, review, or downstream routing boundary is a separate task or phase fan-out item.

### Constraints

- A task performs one coherent execution step and returns that step's complete durable contribution.
- A task does not call another task, phase, or pipeline.
- A task does not persist returned documents or implement retry, timeout, polling-loop, throttling, or local concurrency
  policy.
- Task code does not call `asyncio.wait_for`, sleep for pacing, create semaphores or locks around framework calls, or
  implement throttle loops.
- A task does not silently slice, truncate, sample, or drop document collections.
- If a document collection exceeds the size the business specification accommodates, the task raises `TerminalError`
  with a concrete overflow message.

## Task classes declare fields and async run

Task fields declare configuration and service-client dependencies. Configuration values are set when a phase schedules
the task; service-client fields are populated before the `run` method starts. The `run` method receives pipeline state
as resolved documents.

### Reference

Task classes are frozen model-shaped classes:

```text
class SomeTask(Task):
    dependency: DependencyType

    async def run(self, input_document: InputDocument) -> OutputDocument
```

### Constraints

- A task subclass is frozen and keyword-only.
- A task subclass does not override `__init__`.
- Task fields contain configuration and injected dependencies only.
- Task fields use bare annotations as the visible default.
- `Field(description=...)` is optional for task fields and is not required for injected dependencies.
- Task fields do not have `Document` subclass types, `DocumentBundle` types, or tuples of documents.
- `run` is `async def`.
- `run` receives only resolved document values.
- A value that varies with pipeline state crosses the task boundary as a document.
- A value that configures how the task works is a task field.
- A service client or model capability is a task field supplied before `run` starts.
- `run` does not receive scalar values, enum values, plain content payloads, document bundles, mappings, service
  payloads, or runner configuration objects.
- A task input is not annotated as a bare `Document`.
- A task input is not annotated as a document bundle.
- A task input collection is a tuple, not a list or mapping.
- Phase planning passes task instances to builders.
- A zero-configuration task is still constructed before it is scheduled.
- Phase planning does not pass service-client field values; injected dependencies are populated before `run` starts.
- Task code passes `self.model` to `Contract.execute(model=...)`.
- Task code does not call `str()`, compare, parse, branch on, or pickle `AIModelRef` values.
- Task fields do not contain open handles, futures, coroutines, locks, semaphores, or live client objects carrying
  business state.

### Examples

A task that mixed run-varying configuration into `run()` moves that configuration onto task fields, leaving `run()`
to receive only resolved documents.

```python
# Rejected: configuration and state mixed into the run() signature.
async def run(self, item: ItemDocument, model, round_number, provider_family): ...
```

```python
from ai_pipeline_core import AIModelRef, Task

from review_app.documents import ItemDocument, RequestDocument
from review_app.run_config import ProviderFamily


class GenerateRequestTask(Task):
    """Generate one request, configured by fields and reading only documents."""

    model: AIModelRef
    round_number: int
    provider_family: ProviderFamily

    async def run(self, item: ItemDocument) -> RequestDocument: ...
```

Values that vary with pipeline state cross the task boundary as documents; values that configure how the task works
are task fields supplied by phase planning; run-varying configuration reaches those fields from `RunConfig` through
the phase, never through the `run()` signature.

## Tasks are placement-independent execution units

A task is a self-contained unit of work. Everything it needs is its resolved document inputs and its declared
fields; everything it contributes is the durable documents it returns. Nothing passes from one task to the next, or
from one run to another, through process memory — only through documents and the recorded run. The framework may
execute any task in a fresh compatible process, so a task that depends on in-process state left by earlier work is
incorrect even when it happens to work in a single-process run today.

### Constraints

- A task is correct when executed in any compatible process that can rehydrate its declared inputs and construct its
  declared fields.
- Task correctness depends only on declared document inputs, declared task fields, and shared durable state exposed
  through framework boundaries.
- Task code does not rely on module-level mutable globals, process-local caches, sibling task execution in the same
  process, hidden in-memory accumulators, or current-working-directory side effects as carriers of business state.
- If a task needs external capability with process-local transport state, that state lives behind a service client
  or provider boundary (`api/9-integrations.md`), not in task logic.
- State that must survive a task is returned as documents; state that is not returned is task-local only.

## Required inputs are present when run starts

Absence is handled by phase wiring, not by checks inside the task body. A task starts only when its required inputs are
available.

### Constraints

- A required input receives a resolved document or the task does not start.
- An optional input receives the resolved document or `None`.
- A repeated input receives a tuple.
- An empty `tuple[T, ...]` is a valid value for a required collection input.
- Task code processes zero items as a normal case.
- If empty input cannot produce a useful result, the task raises `TerminalError` with a concrete message.
- A task does not return a default-success document for an empty collection it cannot meaningfully process.
- Task code does not check required inputs for absence.

## Task returns are durable document shapes

The task return annotation is part of the execution contract. Every successful task result is a document, a homogeneous
tuple of documents, or a named bundle of documents. Absence is expressed by phase planning or by a typed outcome
document, not by a successful task returning no state.

### Reference

- Single document result: `AssessmentDocument`.
- Repeated same-type result: `tuple[FindingDocument, ...]`.
- Named multi-output result: `AssessmentOutputs`.

### Constraints

- A successful task returns only documents named as that task's durable contribution.
- A repeated document result uses `tuple[DocumentSubclass, ...]`.
- A multi-output result uses a `DocumentBundle` subclass.
- A task-returned bundle contains document fields or homogeneous document tuple fields.
- A task-returned bundle does not encode task absence with optional fields.
- A task-returned bundle does not re-expose unchanged inputs beside the new contribution.
- A task does not return an input document unchanged.
- If a document requires no changes, the phase routes around the task instead of scheduling a cargo-forwarding task.
- A task may return the same document class as one of its inputs only when it creates a new document identity with new
  content through a provenance-recording factory.
- A task does not return scalar values, enum values, plain content payloads, service payloads, mappings, lists,
  heterogeneous tuples, or `None`.
- A task always returns durable state on success.

## Prompt results become selected documents inside tasks

A task may execute one or more prompt contracts when those interactions serve one coherent durable contribution. Only
prompt results used to create returned documents become durable pipeline state.

### Constraints

- Task code constructs a prompt contract instance and calls `.execute(model=self.model)`.
- Task code supplies the prompt contract's declared inputs.
- Task code supplies no broad active state, unrelated documents, or nearby context the contract did not declare.
- Task code uses `from_prompt_result` whenever a prompt result becomes a document.
- Intermediate prompt results remain task-local unless the business specification names them as durable artifacts.
- A follow-up that needs only a prior answer passes that answer as an ordinary input field (`draft_result.response`);
  a follow-up that must continue the model's own reasoning declares `continues=` and is passed the whole
  `PromptResult` (see _Continuation contracts run inside one task_).
- Task code does not parse model markdown to create business state.
- Task code does not write manual citation markers into model output.

### Examples

```python
from ai_pipeline_core import AIModelRef, Task

from review_app.documents import AssessmentDocument, EvidenceDocument
from review_app.prompt_contracts import AssessEvidenceContract


class AssessEvidenceTask(Task):
    """Run the assessment prompt and persist its result."""

    model: AIModelRef

    async def run(self, evidence: EvidenceDocument) -> AssessmentDocument:
        result = await AssessEvidenceContract(evidence=evidence).execute(model=self.model)
        return AssessmentDocument.from_prompt_result(result=result)
```

```python
from ai_pipeline_core import AIModelRef, Task

from review_app.documents import EvidenceDocument, ReviewPlanDocument
from review_app.prompt_contracts import DraftPlanContract, FinalPlanContract, ReviewPlanContract


class BuildFinalPlanTask(Task):
    """Draft, review, and finalize one plan while returning only the final plan."""

    model: AIModelRef

    async def run(self, evidence: tuple[EvidenceDocument, ...]) -> ReviewPlanDocument:
        draft_result = await DraftPlanContract(evidence=evidence).execute(model=self.model)
        review_result = await ReviewPlanContract(
            draft=draft_result.response,
            evidence=evidence,
        ).execute(model=self.model)
        final_result = await FinalPlanContract(
            draft=draft_result.response,
            review=review_result.response,
            evidence=evidence,
        ).execute(model=self.model)
        return ReviewPlanDocument.from_prompt_result(result=final_result)
```

## Continuation contracts run inside one task

A continuation contract continues the conversation of a declared opener (see
`4-prompt-contracts.md § Multi-step continuation is declared, not improvised`). The lineage is declared on the
contract class; the call site supplies the predecessor by passing the predecessor's whole `PromptResult` — not its
`.response` — into the contract's `PromptResult`-typed field. Continuation stays inside one task.

### Constraints

- A `Continues.once` follow-up is constructed with the opener's `PromptResult` and executed once.
- A `Continues.repeating` step is driven across a bounded number of turns; each turn is passed the prior turn's
  `PromptResult`, and the loop bound comes from the business specification (a literal or a task field), never from
  runtime document content.
- The predecessor passed in is the `PromptResult` value, not `result.response`; passing `.response` is the
  data-only pattern and does not continue the conversation.
- Only the prompt results that become returned documents are durable; intermediate continuation turns stay
  task-local.

### Examples

```python
from ai_pipeline_core import AIModelRef, Task

from review_app.documents import BriefDocument, OpinionDocument, ReviewNotesDocument
from review_app.prompt_contracts import DraftOpinionContract, ReviseOpinionContract


class BuildOpinionTask(Task):
    """Draft an opinion, then revise it once in the same conversation."""

    model: AIModelRef

    async def run(self, brief: BriefDocument, notes: ReviewNotesDocument) -> OpinionDocument:
        draft = await DraftOpinionContract(brief=brief.content.text).execute(model=self.model)
        revised = await ReviseOpinionContract(
            prior=draft,
            notes=notes.content.text,
        ).execute(model=self.model)
        return OpinionDocument.from_prompt_result(result=revised)
```

```python
from ai_pipeline_core import AIModelRef, Task

from review_app.documents import BriefDocument, DraftDocument
from review_app.prompt_contracts import DraftContract, RefineDraftContract


class RefineDraftTask(Task):
    """Draft, then refine the running draft for a bounded number of passes."""

    model: AIModelRef
    max_passes: int

    async def run(self, brief: BriefDocument) -> DraftDocument:
        result = await DraftContract(brief=brief.content.text).execute(model=self.model)
        for _ in range(self.max_passes):
            result = await RefineDraftContract(prior=result).execute(model=self.model)
        return DraftDocument.from_prompt_result(result=result)
```

`ReviseOpinionContract` declares `continues=Continues.once(DraftOpinionContract)`; its first and only continuation
is the draft turn. `RefineDraftContract` declares `continues=Continues.repeating(DraftContract)`; its first turn
continues `DraftContract` and each later turn continues itself, all within the predecessor set
`{DraftContract, RefineDraftContract}`.

## Prompt calls may run concurrently inside one task

Several prompt contracts may run concurrently inside one task when all results belong to one coherent task deliverable.
Standard `asyncio.gather` provides the typed concurrency surface. Concurrent prompt calls do not create separate task
boundaries, separate retry boundaries, or item-level downstream routing points.

### Constraints

- Use `asyncio.gather` only to await independent framework prompt executions that belong to one task result.
- The task fails if any prompt contract in the concurrent set fails.
- Framework task-level retry reruns the whole task boundary and all prompt contracts inside it.
- Use phase fan-out when items need separate retry, separate review boundary, partial degradation, or independent
  downstream routing.
- Task code does not create local worker pools, throttles, or concurrency controls around prompt execution.

### Examples

```python
import asyncio

from ai_pipeline_core import AIModelRef, Task

from review_app.documents import AssessmentDocument, EvidenceDocument
from review_app.prompt_contracts import AssessRiskContract, CombineAssessmentContract, SummarizeEvidenceContract


class BuildAssessmentTask(Task):
    """Build one combined assessment from two prompt judgments."""

    model: AIModelRef

    async def run(self, evidence: tuple[EvidenceDocument, ...]) -> AssessmentDocument:
        summary_result, risk_result = await asyncio.gather(
            SummarizeEvidenceContract(evidence=evidence).execute(model=self.model),
            AssessRiskContract(evidence=evidence).execute(model=self.model),
        )
        combined_result = await CombineAssessmentContract(
            summary=summary_result.response,
            risk=risk_result.response,
        ).execute(model=self.model)
        return AssessmentDocument.from_prompt_result(result=combined_result)
```

## Side effects produce receipt documents

Some tasks cause state outside the pipeline, such as submitting a job, creating a ticket, sending a notification, or
exporting a file. The durable task result is not the side effect itself; it is a receipt document that records the
external identity or status needed for later steps.

### Constraints

- A task that triggers an external side effect returns a receipt or result document.
- A receipt document records the external identity needed for later inspection or collection.
- A receipt document uses causal provenance from the document that caused the side effect.
- Side-effect tasks do not return `None`.
- Task code does not hide side-effect completion in logs, task instance state, module state, or the external system
  alone.

### Examples

```python
from ai_pipeline_core import Task

from review_app.documents import ExportReceipt, ExportReceiptDocument, ReportDocument
from review_app.integrations import ExportClient, ExportReportRequest


class SubmitReportExportTask(Task):
    """Submit one report export and return a receipt."""

    client: ExportClient

    async def run(self, report: ReportDocument) -> ExportReceiptDocument:
        receipt = await self.client.submit_report_export(
            ExportReportRequest(report_id=report.content.report_id),
        )
        return ExportReceiptDocument.from_causal_sources(
            content=ExportReceipt(export_key=receipt.export_key),
            causal_sources=(report,),
        )
```

## Expected item failures become typed outcomes

Expected per-item failures are data. When one item in a collection can be blocked, unavailable, unsupported, or
inconclusive while sibling items may still succeed, the task returns the same document class the successful item path
returns. The content carries the concrete status field that later routing reads.

### Constraints

- A task returns a typed outcome document only for expected item-level results.
- Expected success and expected item failure use the same document class when downstream code handles the collection
  uniformly.
- The outcome content contains the status later routing needs.
- Success payload and failure reason use distinct fields when both can occur in the same outcome type.
- A task raises `RetryableError` or `TerminalError` for failures that are not expected item-level outcomes.
- A task does not return `None` for failed items.

### Examples

```python
from ai_pipeline_core import Task

from review_app.documents import Evidence, EvidenceDocument, EvidenceRequestDocument, EvidenceStatus


class FetchEvidenceTask(Task):
    """Return fetched or blocked evidence as one document type."""

    async def run(self, request: EvidenceRequestDocument) -> EvidenceDocument:
        if request.content.blocked_reason:
            return EvidenceDocument.from_causal_sources(
                content=Evidence(
                    status=EvidenceStatus.BLOCKED,
                    body=None,
                    blocked_reason=request.content.blocked_reason,
                ),
                causal_sources=(request,),
            )
        return EvidenceDocument.from_causal_sources(
            content=Evidence(
                status=EvidenceStatus.FETCHED,
                body=request.content.body,
                blocked_reason=None,
            ),
            causal_sources=(request,),
        )
```

## Insufficient evidence is carried forward, not overwritten

A prompt contract may return an accepted response that abstains — it reports insufficient evidence rather than a
conclusion (see `4-prompt-contracts.md § Abstention is a valid response, not a failure`). Abstention is a
successful result. The task records it as durable state through a typed outcome document instead of discarding it,
substituting a default, or retrying until the model produces a positive answer.

### Constraints

- A task that receives an abstaining response returns a document whose content carries the abstention as a typed
  status, not a fabricated conclusion.
- A task does not overwrite an abstention with a default conclusion, a placeholder, or an empty value.
- A task does not retry a successful abstention in the hope of a positive answer; abstention is not a
  `RetryableError`.
- Expected success and abstention use the same document class when downstream code handles them uniformly, with the
  status field carrying the distinction.
- A task preserves the abstention's explanatory text so a later reader sees why the judgment abstained.

### Examples

```python
from ai_pipeline_core import AIModelRef, Task

from review_app.documents import RiskFindingDocument
from review_app.prompt_contracts import AssessRiskFindingContract


class AssessRiskFindingTask(Task):
    """Run the risk-finding prompt and persist its result, including an abstention."""

    model: AIModelRef

    async def run(self, evidence: RiskFindingDocument) -> RiskFindingDocument:
        result = await AssessRiskFindingContract(evidence=evidence).execute(model=self.model)
        return RiskFindingDocument.from_prompt_result(result=result)
```

### Anti-patterns

Wrong: a task sees `support == "insufficient_evidence"` and substitutes a low-confidence conclusion so downstream
code always has an answer. Correction: return the abstention as durable typed state and let downstream routing
decide what to do with it.

## Item failure has three channels

A failing item in a collection is one of three things, and they are not interchangeable:

- An **expected, business-meaningful outcome** — blocked, unavailable, unsupported, inconclusive — is returned as a
  typed outcome document on the normal item path (see _Expected item failures become typed outcomes_).
- A **transient infrastructure or integration failure** raises `RetryableError`; the framework retries.
- An **unexpected terminal failure** that the run should survive is handled by the phase fan-out, not the task: the
  fan-out is declared with `on_item_failure` set to degrade, and the framework records a typed failure document for
  that item (see `api/7-phases.md`).

### Constraints

- A task does not use a degrade outcome to model an expected business result; expected results are typed outcome
  documents.
- A task does not disguise an infrastructure or integration failure as ordinary data; it raises `RetryableError` or
  `TerminalError`.
- The degrade policy is declared on the phase fan-out, not inside the task; the task raises its typed failure and the
  fan-out decides whether the run survives it.

## Typed exceptions signal task failure

Task exceptions are the public recovery contract at the task boundary. `RetryableError` leaves the task eligible for
framework retry. `TerminalError` stops the task because the current inputs or environment require an external fix.

### Constraints

- `RetryableError` is raised for retry-eligible infrastructure or integration failures.
- `TerminalError` is raised when the current inputs or environment cannot produce a valid task result without an
  external fix.
- A `TerminalError` message states the immediate problem and the concrete fix.
- Missing task implementation stops authoring.
- Application code does not emit placeholder task bodies as package output.
- A task does not catch an unknown exception and return a default success document.
- A task does not implement local retry loops around prompt contracts, service clients, or its own boundary.
- A task does not convert infrastructure failure into an expected item outcome.
