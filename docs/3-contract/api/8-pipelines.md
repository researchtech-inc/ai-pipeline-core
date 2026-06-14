# Pipelines compile the complete run contract

This file defines the public pipeline-level authoring contract for `ai-pipeline-core` applications. It names how a
pipeline binds root inputs, run configuration, ordered phases, pipeline gates, bounded loops, output delivery, and
pre-run validation. It does not define phase internals, task runtime behavior, service-client implementation, or
framework execution internals.

## Pipeline modules import run-shape names

Pipeline code uses framework boundary names to bind root inputs, run configuration, ordered phases, gates, loops, and
final delivery. Public framework names are used directly rather than hidden behind local aliases.

### Reference

```python
from ai_pipeline_core import (
    AIModelRef,
    Document,
    DocumentBundle,
    Field,
    Gate,
    GateBuilder,
    Pipeline,
    PipelineBuilder,
    PipelinePlan,
    RunConfig,
)
```

### Constraints

- Pipeline code declares phases, gates, loops, and delivery.
- Public framework boundary names are used directly.

## A pipeline declares the maximum run shape

Runtime execution may skip a declared phase, stop a declared loop, or deliver an optional absence, but it does not
invent new phases, document routes, or delivery fields after work starts. The pipeline is the declared maximum shape
the framework validates before execution.

### Constraints

- A pipeline subclass has one `build` method.
- `build` is synchronous.
- `build` returns exactly one `PipelinePlan` from `d.outputs`.
- Pipeline subclasses do not declare instance fields.
- Run-varying values come through `run_config`.
- Framework-supplied values come through `build` parameters.
- Run-level routes use gated phases or bounded loops.
- Task-wiring choices inside one business stage belong in phases.

## Pipeline binds input, config, and output bundles

The `Pipeline` type binds three public boundaries: the root input bundle accepted at run start, the configuration object
supplied by the runner, and the output bundle delivered after a successful run. Every document in the root input bundle
is available to the first phase.

### Reference

```python
from ai_pipeline_core import Pipeline, PipelineBuilder, PipelinePlan

from review_app.documents import ReviewInputs, ReviewOutputs, ReviewReportDocument
from review_app.phases import CollectReviewEvidencePhase
from review_app.run_config import ReviewRunConfig


class ReviewPipeline(Pipeline[ReviewInputs, ReviewRunConfig, ReviewOutputs]):
    """Plan one review run."""

    def build(
        self,
        d: PipelineBuilder,
        run_config: ReviewRunConfig,
    ) -> PipelinePlan:
        d.phase(CollectReviewEvidencePhase(model=run_config.analysis_model))
        return d.outputs(report=d.latest_active(ReviewReportDocument))
```

### Constraints

- The first `Pipeline` type argument is the root input bundle type.
- The second `Pipeline` type argument is the run configuration type.
- The third `Pipeline` type argument is the delivered output bundle type.
- The `run_config` parameter in `build` has the declared run configuration type.
- The run configuration parameter is named exactly `run_config`.
- Phases request root documents through `RequiredInput`, `OptionalInput`, or `AllInputs`.
- Pipeline code does not pass root documents into phases manually.
- Pipeline instances are package-level values constructed at import time.
- The pipeline instance is a declaration the framework loads and runs; pipeline code does not start a run on its own.
- Pipeline instances do not carry run-varying fields.
- Run-varying values enter through the `run_config` build parameter and are copied into phase fields when the pipeline
  schedules phases.

## d.phase appends one phase

`d.phase` appends one phase instance to the run plan. Without `when=`, the phase is eligible when execution reaches
that point in the declared order. With `when=`, the phase is eligible only when the supplied gate evaluates true against
documents available at that point.

### Reference

```text
d.phase(phase_instance, *, when: Gate | None = None) -> None
```

### Examples

```python
from ai_pipeline_core import Pipeline, PipelineBuilder, PipelinePlan

from review_app.documents import ReviewDecisionDocument, ReviewInputs, ReviewOutputs, ReviewReportDocument
from review_app.phases import CollectReviewEvidencePhase, RepairDraftPhase
from review_app.run_config import ReviewRunConfig


class ReviewPipeline(Pipeline[ReviewInputs, ReviewRunConfig, ReviewOutputs]):
    """Plan one review run with a gated repair phase."""

    def build(self, d: PipelineBuilder, run_config: ReviewRunConfig) -> PipelinePlan:
        d.phase(CollectReviewEvidencePhase(model=run_config.analysis_model))
        repair_requested = d.gate(
            ReviewDecisionDocument,
            lambda document: document.content.requires_repair,
        ).truthy()
        d.phase(
            RepairDraftPhase(model=run_config.repair_model),
            when=repair_requested,
        )
        return d.outputs(report=d.latest_active(ReviewReportDocument))
```

### Constraints

- `d.phase` receives a phase instance.
- A gated phase that is not eligible contributes no documents to later phases.
- Documents exposed by phase outputs become available to later pipeline gates and final delivery.
- A pipeline keeps directly connected task work inside one phase rather than splitting each task into its own phase.

## d.gate builds phase and loop gates

Pipeline gates are plan values. They are passed to `when=` or `stop_when=` and evaluated by the framework when
execution reaches that point. A field gate is anchored by a document class and a typed selector lambda over that
document's `.content`.

### Reference

```text
d.gate[D: Document, V](document_class: type[D], selector: Callable[[D], V]) -> GateBuilder[V]
d.is_present[D: Document](document_class: type[D]) -> Gate
d.not_(gate: Gate) -> Gate
```

### Examples

```python
from ai_pipeline_core import Pipeline, PipelineBuilder, PipelinePlan

from review_app.documents import ReviewDecisionDocument, ReviewInputs, ReviewOutputs, ReviewReportDocument, ReviewStatus
from review_app.phases import CollectReviewEvidencePhase, CreateReviewReportPhase, NotifyMissingReportPhase
from review_app.run_config import ReviewRunConfig


class ReviewPipeline(Pipeline[ReviewInputs, ReviewRunConfig, ReviewOutputs]):
    """Plan one review run with pipeline gates."""

    def build(self, d: PipelineBuilder, run_config: ReviewRunConfig) -> PipelinePlan:
        d.phase(CollectReviewEvidencePhase(model=run_config.analysis_model))
        approved = d.gate(
            ReviewDecisionDocument,
            lambda document: document.content.status,
        ).equals(ReviewStatus.APPROVED)
        d.phase(
            CreateReviewReportPhase(model=run_config.publish_model),
            when=approved,
        )
        d.phase(
            NotifyMissingReportPhase(),
            when=d.not_(d.is_present(ReviewReportDocument)),
        )
        return d.outputs(report=d.latest_active(ReviewReportDocument))
```

### Constraints

- `d.gate` points at a typed field on a document type available to the pipeline.
- Selector lambdas are pure attribute access over `.content`.
- The framework rejects selector lambdas containing operators, calls, or branches at plan compile time.
- The document class argument to `d.is_present` returns a gate usable in `when=`, `stop_when=`, or `d.not_`.
- `d.not_(gate)` returns a gate usable anywhere a gate is accepted.
- Equality gates compare against values accepted by the selected field type.
- Truth gates are used only for boolean-shaped fields.
- Compound route logic is represented by one typed routing field produced by an earlier phase.

## d.loop declares bounded repetition

`d.loop` repeats a declared phase sequence up to a fixed maximum count. The loop body declares the phase structure for
one round. `stop_when` is evaluated after each completed round against the documents available after that round; when
it becomes true, no later declared rounds run.

### Reference

```text
d.loop(
    *,
    name: str,
    max_rounds: int,
    build_round: Callable[[PipelineBuilder, int], None],
    when: Gate | None = None,
    stop_when: Gate | None = None,
) -> None
```

### Examples

```python
from ai_pipeline_core import Pipeline, PipelineBuilder, PipelinePlan

from review_app.documents import DraftDocument, ReviewDecisionDocument, ReviewInputs, ReviewOutputs
from review_app.phases import ReviewRoundPhase
from review_app.run_config import ReviewRunConfig


class ReviewPipeline(Pipeline[ReviewInputs, ReviewRunConfig, ReviewOutputs]):
    """Plan review rounds until approval or the configured bound."""

    def build(self, d: PipelineBuilder, run_config: ReviewRunConfig) -> PipelinePlan:
        def build_review_round(
            round_builder: PipelineBuilder,
            round_number: int,
        ) -> None:
            round_builder.phase(
                ReviewRoundPhase(
                    round_number=round_number,
                    model=run_config.review_model,
                ),
            )

        d.loop(
            name="review_until_approved",
            max_rounds=run_config.max_review_rounds,
            build_round=build_review_round,
            when=d.is_present(DraftDocument),
            stop_when=d.gate(
                ReviewDecisionDocument,
                lambda document: document.content.approved,
            ).truthy(),
        )
        return d.outputs(draft=d.latest_active(DraftDocument))
```

A loop round that revises a current artifact re-emits the current handoff. The revision task creates the next draft
with `DraftDocument.from_prior_revision` using the prior draft, replacement content, and immediate sources.

```python
from ai_pipeline_core import AIModelRef, Phase, PhaseBuilder, PhasePlan, RequiredInput

from review_app.documents import DraftDocument, ReviewDocument
from review_app.tasks import ReviewDraftTask, ReviseDraftTask


class ReviewRoundPhase(Phase):
    """Review the current draft and revise it when repair is required."""

    model: AIModelRef
    round_number: int

    def build(
        self,
        f: PhaseBuilder,
        draft: RequiredInput[DraftDocument],
    ) -> PhasePlan:
        review = f.step(
            ReviewDraftTask(model=self.model, round_number=self.round_number),
            draft=draft,
        )
        revised = f.step(
            ReviseDraftTask(model=self.model, round_number=self.round_number),
            prior=draft,
            review=review,
            when=f.gate(review, lambda document: document.content.requires_repair).truthy(),
        )
        next_draft = f.first_present(revised, draft)
        return f.outputs(draft=next_draft, review=review)
```

### Constraints

- `name` is keyword-only.
- `name` is a stable identifier and is unique among loops in the pipeline.
- `when=` is evaluated before the first round; when false, no loop round runs.
- `build_round` is invoked once per declared round number from `1` through `max_rounds` at plan compile time.
- `round_number` is one-based.
- `max_rounds` comes from an integer literal copied from the business specification, a static pipeline constant, or a
  `RunConfig` field.
- Application code does not invent loop bounds.
- If the business specification names no loop bound, implementation stops for that loop boundary until the
  specification supplies one.
- `max_rounds` does not come from a runtime document, prompt result, service result, or document query.
- The loop body declares phases and gates used by those phase declarations.
- The loop body does not call `d.outputs`, declare nested loops, inspect runtime documents, call tasks, execute prompt
  contracts, or create documents.

## Active documents are phase outputs available to delivery

Final delivery reads active documents, not the durable store directly. An active document is a root input document or a
document named by `f.outputs` in a phase that has completed. Revision history remains document metadata; active
selection is based on pipeline availability and exact document type.

### Reference

```text
d.latest_active[D: Document](document_class: type[D]) -> D
d.all_active[D: Document](document_class: type[D]) -> tuple[D, ...]
```

`d.latest_active(T)` resolves to `T` when bound to a required output field and to `T | None` when bound to an optional
output field. `d.all_active(T)` resolves to `tuple[T, ...]`.

### Examples

```python
report_ref = d.latest_active(ReviewReportDocument)
finding_refs = d.all_active(ReviewFindingDocument)
```

If `CollectEvidencePhase` outputs one `EvidenceDocument` and two later loop rounds each output a new
`EvidenceDocument`, `d.latest_active(EvidenceDocument)` binds the second loop-round document. `d.all_active` binds the
collect output followed by both loop-round outputs.

### Constraints

- A required delivered field uses `d.latest_active`.
- A field typed as an optional concrete document class may bind `d.latest_active` for that class and receives `None`
  when no available document of that type exists.
- A tuple output uses `d.all_active` and may receive an empty tuple.
- Pipeline code does not query the durable store to build delivered outputs.

### Invariants

- Available documents are ordered by phase declaration order and loop iteration order.
- `d.latest_active(T)` selects the most recently produced available document of exact type `T`.
- `d.all_active(T)` returns available documents of exact type `T` in output order.

## d.outputs names the public delivery contract

`d.outputs` closes the pipeline plan and binds document refs to the fields declared by the pipeline output bundle. The
delivered bundle is the caller-facing result of a successful run.

### Reference

```text
d.outputs(report=report_ref) -> PipelinePlan
d.outputs() -> PipelinePlan
```

### Examples

```python
return d.outputs(
    report=d.latest_active(ReviewReportDocument),
    findings=d.all_active(ReviewFindingDocument),
)
```

### Constraints

- `d.outputs` appears once.
- `build` returns the `PipelinePlan` produced by `d.outputs`.
- Every output bundle field is named, including optional fields.
- Optional delivered fields are explicitly optional in the output bundle type.
- Delivery reads phase outputs only.
- `d.outputs()` is used only when the output bundle declares no fields.

## RunConfig defines runner-supplied configuration

`RunConfig` is the configuration contract the runner supplies when a run starts. Pipeline code reads configuration
during plan construction and passes selected values into phase constructors. Tasks receive those selected values through
phase planning and task fields.

### Examples

```python
from ai_pipeline_core import AIModelRef, Field, RunConfig


class ReviewRunConfig(RunConfig):
    """Runner-supplied configuration for one review run."""

    analysis_model: AIModelRef = Field(
        description="Model capability used by analysis prompt contracts.",
    )
    max_review_rounds: int = Field(
        default=2,
        description="Maximum number of review loop rounds.",
    )
```

### Constraints

- Pipeline code receives the run configuration through the `run_config` parameter.
- Application code declares `RunConfig` fields; runners supply concrete values when a run starts.
- Application code does not load environment variables, parse model strings, or construct `RunConfig` instances inside
  pipeline, phase, or task code.
- Pipeline code passes concrete option values to phase constructors.
- Pipeline code does not pass the whole configuration object into a phase to hide the phase's public configuration
  needs.
- `AIModelRef` values are supplied through `RunConfig`.
- Pipeline code passes model capability to phases as configuration.
- Pipeline code treats model capability as an opaque value and passes it to phases as configuration.
- The task contract owns the forbidden operations on `AIModelRef`; pipeline code does not inspect model capability.
