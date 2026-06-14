# Phases compile task plans from planning refs

This file defines the public phase authoring contract for `ai-pipeline-core` applications. A phase receives planning
refs, schedules task instances, applies local conditions, schedules child pipelines, and names the documents visible to
later phases. It does not define task bodies, pipeline ordering, service-client behavior, or framework internals.

## Phase authors import planning names

Phase authors use one base class, one builder, one plan return type, planning-ref input wrappers, gate values, and ref
types. The wrappers keep requested documents visible in the `build` signature.

### Reference

```python
from ai_pipeline_core import (
    AllInputs,
    CollectionRef,
    Document,
    DocumentRef,
    Gate,
    ItemFailure,
    ItemFailurePolicy,
    OptionalInput,
    OptionalRef,
    Phase,
    PhaseBuilder,
    PhasePlan,
    RequiredInput,
)
```

`Phase` is subclassed by application code. `PhaseBuilder` is passed to `build`. `PhasePlan` is returned from
`f.outputs`. `RequiredInput[T]`, `OptionalInput[T]`, and `AllInputs[T]` request documents made available by root inputs
or earlier phase outputs.

## Phases compose tasks into one business stage

A phase is one named unit of business progress. It may schedule several task instances, child pipelines, local
branches, fan-outs, accumulations, and fallback paths, but those pieces serve one stage of the pipeline. The phase
exposes only the documents named in `f.outputs`.

### Constraints

- A phase subclass is frozen and keyword-only.
- A phase subclass does not override `__init__`.
- Phase fields use bare annotations as the visible default.
- `Field(description=...)` is optional for phase fields and is not required for injected dependencies or static bounds.
- A phase coordinates task instances and child pipeline boundaries; it does not create documents directly.
- Later phases rely only on documents named by `f.outputs`.
- A phase may declare `estimated_minutes: ClassVar[int]` — an authored estimate of the phase's wall-clock duration
  that the runtime surfaces in the pipeline's integration descriptor and reconstruction (`runtime-api/2-run-control.md`,
  `runtime-api/4-run-record-read-model.md`). It is optional metadata for planning, not a bound the framework
  enforces; when a phase omits it, the descriptor reports it absent or `0`.

## Phase build declares a plan

`build` declares possible work and wiring. It does not perform task work, model calls, service calls, document
construction, or document loading. When a phase needs runner-supplied configuration, the pipeline supplies those values
as phase fields before `build` runs.

### Reference

```python
from ai_pipeline_core import Phase, PhaseBuilder, PhasePlan, RequiredInput

from review_app.documents import DraftDocument, ReviewDocument
from review_app.tasks import ReviewDraftTask


class ReviewPhase(Phase):
    """Review the current draft and expose the review decision."""

    def build(
        self,
        f: PhaseBuilder,
        draft: RequiredInput[DraftDocument],
    ) -> PhasePlan:
        review = f.step(ReviewDraftTask(), draft=draft)
        return f.outputs(review=review)
```

### Constraints

- `build` is synchronous.
- `build` contains builder calls and local plan-shaping helpers only.
- Python `if`, `for`, and `while` statements do not test, iterate over, or branch on planning refs.
- Configuration comes from phase fields supplied by the pipeline, never from resolved document data.

## Phase inputs are planning refs

Phase inputs request documents already available to this phase. The wrapper names whether the phase requires one
document, accepts absence, or consumes every available document of a type.

### Reference

- `RequiredInput[T]` requests one required available document of type `T`.
- `OptionalInput[T]` requests one available document of type `T` when present and absence otherwise.
- `AllInputs[T]` requests every available document of type `T`.

The input wrappers **are** planning refs: `RequiredInput[T]` is a `DocumentRef[T]`, `OptionalInput[T]` is an
`OptionalRef[T]`, and `AllInputs[T]` is a `CollectionRef[T]`. A wrapper is therefore accepted anywhere the
corresponding ref type is declared — `f.item(all_inputs)`, `f.for_each(over=all_inputs)`,
`f.accumulate(initial=required_input)`, `f.step(x=required_input)` — and a standard type checker sees it as that ref
type with no framework plugin (`api/1-application.md § Standard type checking covers public declarations`).

### Constraints

- A phase input names a document type, not a scalar value.
- A document is available to phase `P` when it is a root document from the pipeline input bundle or it was named in
  `f.outputs(...)` of a phase scheduled before `P`.
- Different semantic roles use different document types even when their content fields are similar.
- `RequiredInput[T]` and `OptionalInput[T]` resolve the latest available document of exact type `T`.
- `AllInputs[T]` resolves available documents of exact type `T` in output order.
- `AllInputs[T]` returns an empty tuple when no available documents of type `T` exist.

### Invariants

- `AllInputs[T]` order is declaration-then-iteration order.
- Documents are returned in the order they were named by `f.outputs` of completed phases.
- Within a pipeline loop, documents are ordered by round number.
- Within `f.for_each`, documents are ordered by input-collection order.

### Examples

```python
from ai_pipeline_core import OptionalInput, Phase, PhaseBuilder, PhasePlan, RequiredInput

from review_app.documents import DraftDocument, ReviewDocument
from review_app.tasks import PublishDraftTask


class PublishDraftPhase(Phase):
    """Publish a draft with an optional review document."""

    def build(
        self,
        f: PhaseBuilder,
        draft: RequiredInput[DraftDocument],
        review: OptionalInput[ReviewDocument],
    ) -> PhasePlan:
        published = f.step(PublishDraftTask(), draft=draft, review=review)
        return f.outputs(draft=published)
```

`PublishDraftTask.run` accepts `review: ReviewDocument | None` and uses its no-review path when absence resolves.

## f.step schedules one task instance

`f.step` schedules one task instance and returns a planning ref for that step result. Task input keywords match the
scheduled task's `run` parameters. A task that returns a document bundle exposes bundle fields by attribute name, such
as `review_outputs.report`.

### Reference

```text
f.step(ReviewDraftTask(), draft=draft) -> DocumentRef[ReviewDocument]
f.step(ReviewDraftTask(), draft=draft, when=repair_requested) -> OptionalRef[ReviewDocument]
```

### Constraints

- The first argument is a task instance.
- Zero-configuration tasks are still passed as instances.
- A task class is not passed in place of an instance.
- Child pipelines are scheduled with `f.child_pipeline`, not with `f.step`.

## f.child_pipeline schedules one child pipeline boundary

A child pipeline is a pipeline boundary scheduled inside a phase. The parent phase supplies the child pipeline class,
the child run configuration, and the child input bundle built from planning refs.

### Reference

```text
f.child_pipeline(
    ChildPipelineClass,
    *,
    run_config: ChildRunConfig,
    inputs: ChildInputs,
) -> ChildOutputs
```

### Examples

```python
from ai_pipeline_core import AIModelRef, Phase, PhaseBuilder, PhasePlan, RequiredInput

from review_app.documents import VendorRequestDocument, VendorReviewInputs
from review_app.pipelines import VendorReviewPipeline
from review_app.run_config import VendorReviewRunConfig


class ParentReviewPhase(Phase):
    """Run one vendor review as a child pipeline."""

    child_review_model: AIModelRef

    def build(
        self,
        f: PhaseBuilder,
        vendor_request: RequiredInput[VendorRequestDocument],
    ) -> PhasePlan:
        child_outputs = f.child_pipeline(
            VendorReviewPipeline,
            run_config=VendorReviewRunConfig(analysis_model=self.child_review_model),
            inputs=VendorReviewInputs(request=vendor_request),
        )
        return f.outputs(review=child_outputs.review)
```

### Constraints

- The child pipeline argument is the concrete pipeline class.
- The framework derives the child input bundle type, run configuration type, and output bundle type from the pipeline
  class.
- The child input bundle may contain planning refs because `f.child_pipeline` is an explicit builder boundary.
- Task code does not invoke child pipelines directly.
- `f.child_pipeline` is the one child-pipeline boundary an author writes; the child pipeline argument is the
  concrete child `Pipeline` class. Whether that child executes in-process or as a separately-deployed run is a
  framework-owned placement decision, not a difference the author codes against; the placement-independent
  contract — local and remote behaving the same from the author's and the operator's point of view — is
  `runtime-api/6-child-pipelines.md`.

## f.for_each schedules bounded parallel work

`f.for_each` schedules one task execution for each item in a homogeneous collection ref. It is the phase shape for
repeated item work that needs separate retry boundaries and durable item results. The iterated item is passed through a
typed marker rather than a string parameter name.

### Reference

```text
f.item[D: Document](collection: CollectionRef[D]) -> DocumentRef[D]
f.for_each[D: Document, R](
    task_instance,
    *,
    over: CollectionRef[D],
    max_items: int,
    when: Gate | None = None,
    on_item_failure: ItemFailurePolicy = ItemFailurePolicy.FAIL_RUN,
    failure_document: type[Document] | None = None,
) -> CollectionRef[R]
```

Additional keyword arguments to `f.for_each` are concrete task `run` parameters. A parameter that receives the iterated
item uses `f.item(over)`. The `on_item_failure` and `failure_document` arguments select how a single item's failure
is handled (see _Fan-out item failure is fail-closed by default_).

### Examples

```python
from ai_pipeline_core import AllInputs, Phase, PhaseBuilder, PhasePlan

from review_app.documents import EvidenceDocument
from review_app.tasks import VerifyEvidenceTask


class VerifyEvidencePhase(Phase):
    """Verify each evidence document under a static bound."""

    max_evidence_items: int

    def build(
        self,
        f: PhaseBuilder,
        evidence: AllInputs[EvidenceDocument],
    ) -> PhasePlan:
        findings = f.for_each(
            VerifyEvidenceTask(),
            over=evidence,
            evidence=f.item(evidence),
            max_items=self.max_evidence_items,
        )
        return f.outputs(findings=findings)
```

### Constraints

- `over=` is a homogeneous collection planning ref.
- `f.item(over)` supplies the task `run` parameter that receives one item.
- `max_items=` is required and is a positive integer value available at plan construction.
- Legal bound sources are integer literals copied from the business specification or phase fields supplied by the
  pipeline.
- Pipeline code may copy a `RunConfig` value into a phase field used as `max_items=`.
- Application code does not invent repeated-work bounds.
- If the business specification names no repeated-work bound, implementation stops for that boundary until the
  specification supplies one.
- Bounds are not computed from resolved document payloads.
- A collection that exceeds its bound is rejected by the framework.
- The framework does not silently truncate repeated work.
- Item task implementations do not rely on item execution order.
- A collection of bundle results projects one field with `collection.project(lambda output: output.report)`.

### Invariants

- Each item execution receives remaining keyword inputs unchanged.
- Fan-out result order follows input order.
- When `over=` resolves to an empty collection, no item executions are scheduled.
- An empty fan-out returns a collection ref with length zero.

## f.for_each_product schedules bounded cross-product work

`f.for_each_product` schedules one task execution for each item in the cross product of two or more homogeneous
collection refs. It is the phase shape for matrix work over independent axes — every combination of one axis with
another — that a single `f.for_each` cannot express without flattening the product into scaffolding.

### Reference

```text
f.for_each_product[R](
    task_instance,
    *,
    over: tuple[CollectionRef[Any], ...],
    max_items: int,
    when: Gate | None = None,
    on_item_failure: ItemFailurePolicy = ItemFailurePolicy.FAIL_RUN,
    failure_document: type[Document] | None = None,
) -> CollectionRef[R]
```

Each axis supplies one task `run` parameter through `f.item(axis_collection)`; remaining keyword arguments are
concrete task `run` parameters.

### Examples

```python
from ai_pipeline_core import AllInputs, Phase, PhaseBuilder, PhasePlan

from review_app.documents import ProfileDocument, ScenarioDocument
from review_app.tasks import SimulateReactionTask


class SimulateReactionsPhase(Phase):
    """Simulate every profile's reaction to every scenario under a static bound."""

    max_simulations: int

    def build(
        self,
        f: PhaseBuilder,
        profiles: AllInputs[ProfileDocument],
        scenarios: AllInputs[ScenarioDocument],
    ) -> PhasePlan:
        reactions = f.for_each_product(
            SimulateReactionTask(),
            over=(profiles, scenarios),
            profile=f.item(profiles),
            scenario=f.item(scenarios),
            max_items=self.max_simulations,
        )
        return f.outputs(reactions=reactions)
```

### Constraints

- `over=` is a tuple of two or more homogeneous collection refs.
- Each axis supplies one task `run` parameter through `f.item(axis_collection)`.
- `max_items=` is required and bounds the size of the product — the count of scheduled executions — so the maximum
  fan-out and cost stay knowable before the run starts.
- Legal `max_items` sources are integer literals copied from the business specification or phase fields supplied by
  the pipeline; the bound is never computed from resolved document content.
- A product that exceeds `max_items` is rejected by the framework; the framework does not silently truncate.
- A dependent or derived product — where one axis is computed from another and cannot be stated as an independent
  collection — is materialized by a prior task into a typed collection of job documents, and a later phase fans out
  over it with `f.for_each` (see `api/2-documents.md`).

### Invariants

- When any axis resolves to an empty collection, the product is empty and no executions are scheduled.
- Result order follows the nested declaration order of `over=`.

## Fan-out item failure is fail-closed by default

By default, one item's terminal failure fails the whole fan-out and the run. A fan-out may instead be declared to
survive an item's failure, in which case the failed item becomes a durable, typed failure document of an
author-declared type while the surviving items continue.

### Reference

`ItemFailurePolicy` is a closed set:

- `FAIL_RUN` — one item's terminal failure fails the phase and the run. This is the default.
- `DEGRADE` — a terminal item failure becomes a typed failure document, and the fan-out continues with the surviving
  items.

Under `DEGRADE`, the framework — not the failed task, which raised rather than returned — must record the failure
document, so the document's content is a framework-owned shape: `ItemFailure`, a `FrozenBaseModel` exposing
`error_message: str` (the terminal failure message) and `failed_item_id: str` (the identity of the input item
document that failed). The author declares the document *type* so later phases can select degraded items by type;
the content is `ItemFailure` and the framework fills it:

```text
class EvidenceFailureDocument(Document[ItemFailure]):
    """Records one evidence item that failed terminally under a degrading fan-out."""
```

### Constraints

- `on_item_failure` defaults to `FAIL_RUN`; a fan-out is fail-closed unless it opts into `DEGRADE`.
- An item whose task raises `RetryableError` is retried first; only a `TerminalError` that survives retry is subject
  to the policy.
- Under `DEGRADE`, `failure_document=` is required and names a concrete `Document[ItemFailure]` subclass; the
  framework records one such document for each item that fails terminally, populating its `ItemFailure` content from
  the surviving `TerminalError` and recording the failed input item document as a causal source.
- The author does not author the failure document's content fields and does not construct it; `ItemFailure` is
  framework-owned, so the framework can build it from the caught failure without the application minting content it
  cannot know.
- A degraded item is never silently dropped: each failed item is a durable typed document, recorded and available to
  later phases by its type.
- The returned collection ref holds the surviving results; degraded items are read by later phases through their
  declared failure document type.
- `DEGRADE` handles unexpected terminal failures; an expected business outcome is a typed outcome document on the
  normal item path (`api/6-tasks.md`).
- `f.accumulate` does not take `on_item_failure` or `failure_document`; a serial fold has no per-item survive choice.

## f.accumulate schedules bounded serial work

`f.accumulate` threads one evolving document through many item executions. Each scheduled task receives the current
accumulator and one item, then returns the next accumulator. The accumulator and item are passed through typed markers
rather than string parameter names.

### Reference

```text
f.accumulator[A: Document](initial: DocumentRef[A]) -> DocumentRef[A]
f.accumulate[D: Document, A: Document](
    task_instance,
    *,
    over: CollectionRef[D],
    initial: DocumentRef[A],
    max_items: int,
    when: Gate | None = None,
) -> DocumentRef[A]
```

Additional keyword arguments to `f.accumulate` are concrete task `run` parameters. The accumulator parameter uses
`f.accumulator(initial)`, and the item parameter uses `f.item(over)`.

### Examples

```python
from ai_pipeline_core import AllInputs, Phase, PhaseBuilder, PhasePlan, RequiredInput

from review_app.documents import AssessmentStateDocument, FindingDocument
from review_app.tasks import ApplyFindingTask


class ApplyFindingsPhase(Phase):
    """Apply findings to one evolving assessment state."""

    max_findings: int

    def build(
        self,
        f: PhaseBuilder,
        state: RequiredInput[AssessmentStateDocument],
        findings: AllInputs[FindingDocument],
    ) -> PhasePlan:
        updated_state = f.accumulate(
            ApplyFindingTask(),
            over=findings,
            initial=state,
            state=f.accumulator(state),
            finding=f.item(findings),
            max_items=self.max_findings,
        )
        return f.outputs(state=updated_state)
```

### Constraints

- The scheduled task returns the same document type as the accumulator.
- `f.accumulator(initial)` supplies the task `run` parameter that receives the current accumulator.
- `f.item(over)` supplies the task `run` parameter that receives one item.
- `max_items=` is required and is a positive integer value available at plan construction.
- Legal bound sources are integer literals copied from the business specification or phase fields supplied by the
  pipeline.
- Pipeline code may copy a `RunConfig` value into a phase field used as `max_items=`.
- Application code does not invent repeated-work bounds.
- If the business specification names no repeated-work bound, implementation stops for that boundary until the
  specification supplies one.
- Bounds are not computed from resolved document payloads.
- A collection that exceeds its bound is rejected by the framework.
- The framework does not silently truncate repeated work.

### Invariants

- Accumulation follows the order of the collection ref supplied to `over=`.
- When `over=` resolves to an empty collection, no fold iterations are scheduled.
- An empty accumulation returns the initial accumulator ref.

## f.branch selects one local result

`f.branch` selects one local branch inside the phase and returns a planning ref for the branch result. `cases` maps
each closed branch key to a callable. Each callable receives a branch-local `PhaseBuilder` and returns a planning ref
of the same shape every other branch returns.

### Reference

```text
f.branch[D: Document, K, R](
    document_ref: DocumentRef[D],
    selector: Callable[[D], K],
    *,
    cases: dict[K, Callable[[PhaseBuilder], DocumentRef[R]]],
    default: Callable[[PhaseBuilder], DocumentRef[R]] | None = None,
) -> DocumentRef[R]
```

### Examples

```python
from ai_pipeline_core import AllInputs, Phase, PhaseBuilder, PhasePlan, RequiredInput

from review_app.documents import EvidenceDocument, RequestDocument, RouteDocument
from review_app.tasks import FullReviewTask, LightweightReviewTask


class SelectReviewPhase(Phase):
    """Select one local review path."""

    def build(
        self,
        f: PhaseBuilder,
        request: RequiredInput[RequestDocument],
        evidence: AllInputs[EvidenceDocument],
        route: RequiredInput[RouteDocument],
    ) -> PhasePlan:
        review = f.branch(
            route,
            lambda document: document.content.use_full_review,
            cases={
                False: lambda branch: branch.step(LightweightReviewTask(), request=request),
                True: lambda branch: branch.step(FullReviewTask(), request=request, evidence=evidence),
            },
        )
        return f.outputs(review=review)
```

### Constraints

- Branch callables are declared inside the `build` method that uses them.
- `f.branch` selects between local variants inside one business stage.
- A route that selects between business stages, phase sequences, or loop bodies uses pipeline-level gated phases or
  `d.loop`.
- The selector is a pure attribute lambda over the selected document's `.content`.
- Branch keys are closed values such as `StrEnum`, `Literal`, or `bool`.
- Free-form `str` and unconstrained integer fields are not branch keys.
- Every branch returns the same planning-ref shape.
- If the runtime value matches no branch and no default is declared, branch resolution fails before downstream tasks
  consume the missing result.

## Phase gates keep absence explicit

Conditional work produces optional planning refs. A downstream task either accepts absence, uses a fallback, or waits
for a definite ref. Phase gates use typed lambdas over document refs and do not use string selectors.

### Reference

```text
f.gate(review: DocumentRef[ReviewDocument], selector: Callable[[ReviewDocument], bool]).truthy() -> Gate
f.gate(review: DocumentRef[ReviewDocument], selector: Callable[[ReviewDocument], bool]).falsy() -> Gate
f.gate(review: DocumentRef[ReviewDocument], selector: Callable[[ReviewDocument], ReviewStatus]).equals(value) -> Gate
f.not_(gate: Gate) -> Gate
f.first_present(*refs: DocumentRef[T] | OptionalRef[T]) -> DocumentRef[T]
f.allow_absent(ref: OptionalRef[T]) -> OptionalRef[T]
```

### Examples

```python
from ai_pipeline_core import Phase, PhaseBuilder, PhasePlan, RequiredInput

from review_app.documents import DraftDocument, ReviewDocument
from review_app.tasks import RepairDraftTask


class RepairDraftPhase(Phase):
    """Repair a draft when review requires it."""

    def build(
        self,
        f: PhaseBuilder,
        draft: RequiredInput[DraftDocument],
        review: RequiredInput[ReviewDocument],
    ) -> PhasePlan:
        repaired = f.step(
            RepairDraftTask(),
            draft=draft,
            review=review,
            when=f.gate(review, lambda document: document.content.requires_repair).truthy(),
        )
        final_draft = f.first_present(repaired, draft)
        return f.outputs(draft=final_draft, repair=f.allow_absent(repaired))
```

### Constraints

- Conditional builder calls receive conditions through `when=`.
- A required downstream input does not consume an absent ref directly.
- `f.first_present` is used when later work needs a definite ref after optional work.
- `f.allow_absent` is used only when the phase output contract permits absence.
- Truth gates are used only on boolean fields.
- Selector lambdas are pure attribute access over `.content`.
- The framework rejects selector lambdas containing operators, calls, or branches at plan compile time.
- Phase gates use planning refs produced or received inside the phase.
- Pipeline gates use document classes over phase outputs.

## f.outputs defines the phase boundary

`f.outputs` closes the phase plan and names the documents that become the phase contribution. Documents produced by
internal steps remain durable, but they are not visible to later phases unless they are named here. Later phases request
available documents by exact document type through planning refs.

### Reference

```text
f.outputs(review=review_ref) -> PhasePlan
f.outputs() -> PhasePlan
```

### Constraints

- Output field names are stable phase contract names.
- A phase outputs only documents named as that phase's handoff to later phases, pipeline gates, or final delivery.
- A required phase output names a definite ref.
- An optional phase output uses `f.allow_absent`.
- Intermediate task results are omitted unless a later phase, pipeline gate, or final delivery explicitly consumes
  that document type.
- A phase that intentionally hands off no documents returns `f.outputs()`.
- A phase does not forward inherited input refs unchanged through `f.outputs`; later phases request available documents
  through their own phase inputs.
- A ref produced by `f.first_present`, `f.branch`, or a phase-internal selection helper may be output when that selected
  handoff is the phase contribution, even if one branch resolves to an input document.
- Raw input refs cannot be re-bound as phase outputs.

### Invariants

- Documents named in `f.outputs` become available to later phases.
- Documents omitted from `f.outputs` remain durable but internal to the phase.
