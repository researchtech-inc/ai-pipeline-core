# Applications declare compiled pipeline packages

This file defines the public application-package contract for `ai-pipeline-core` authors. It names the package roles,
fixed layout, build order, validation boundary, type-checking posture, and minimal loadable package shape that
application code may rely on. It does not define the detailed contracts for documents, content models, prompt
contracts, model tools, tasks, phases, pipelines, or integrations.

The `api/` files (1–9) are the authoring surface only — the part an agent reads to write a pipeline package.
Running a pipeline, reading its record, the runner, and the database API are not part of this surface; they belong
to the advanced programmatic surface in `advanced-api/`.

The backends the framework runs on never surface in this authoring surface. An author neither names nor configures
which persistence store, orchestrator/runtime backend, event transport, or run-log store the framework uses; the
authoring contract is the same regardless of those choices, which are operator configuration (`5-configuration.md`).
What an author selects is the per-judgment model, supplied as an opaque `AIModelRef` through `RunConfig`, and
nothing about the infrastructure beneath it.

## Application packages import boundary names from ai_pipeline_core

Application packages use the top-level framework package as the import boundary. Framework internals are not part of
the application contract, and local aliases hide the role a class is meant to play.

### Reference

```python
from ai_pipeline_core import (
    AIModelRef,
    AllInputs,
    CitedText,
    Document,
    DocumentBundle,
    Field,
    FrozenBaseModel,
    Phase,
    PhaseBuilder,
    PhasePlan,
    Pipeline,
    PipelineBuilder,
    PipelinePlan,
    PromptContract,
    RequiredInput,
    RunConfig,
    Task,
)
```

### Constraints

- Application code imports framework boundary names directly from `ai_pipeline_core`.
- Application code does not import framework internals to complete a pipeline package.
- Application packages do not define local aliases for framework roles, builders, plans, refs, model capability, tools,
  tasks, phases, pipelines, service clients, or errors.

## Applications have five authoring roles

A package has five core roles. A document holds durable state, a prompt contract defines one model-mediated judgment, a
task turns resolved documents into durable results, a phase compiles task work into one business stage, and a pipeline
compiles the complete run shape. Model tools and integrations are supporting boundaries used by prompt contracts and
tasks when the package needs external capability.

### Constraints

- Application code implements only the business-logic boundaries named by the package specification.
- Document classes use the `*Document` suffix.
- Prompt contract classes use the `*Contract` suffix.
- Task classes use the `*Task` suffix.
- Phase classes use the `*Phase` suffix.
- Pipeline classes use the `*Pipeline` suffix.
- Run configuration classes use the `*RunConfig` suffix.
- Input bundle classes use the `*Inputs` suffix.
- Output bundle classes use the `*Outputs` suffix.
- Model tool classes use the `*Tool` suffix.
- Service client classes use the `*Client` suffix.
- Methodology classes use the `*Methodology` suffix.
- The framework rejects mismatched role suffixes at import time.
- Application code does not invent documents, prompt contracts, tasks, phases, routes, bounds, identifiers, outputs, or
  pipeline branches because they seem locally useful.
- A missing or ambiguous business-logic boundary stops implementation until the package specification supplies it.

## Application package layout is fixed

The package layout makes authoring roles visible before any file is opened. A fresh agent can locate the correct home
for a change from the path alone.

### Reference

- `review_app/__init__.py` exports the package pipeline.
- `review_app/run_config.py` declares runner-supplied configuration.
- `review_app/documents/` declares content models, document classes, and bundles.
- `review_app/prompt_contracts/` declares prompt contracts and output models.
- `review_app/tasks/` holds task classes.
- `review_app/phases/` holds phase classes.
- `review_app/pipeline.py` declares the pipeline instance.
- `review_app/pipelines/` holds child pipeline classes when a package declares reusable child pipelines.
- `review_app/tools/` holds model tools.
- `review_app/integrations/` holds service clients and polling clients.
- `review_app/tests/` holds the application's tests. Tests are not a public authoring role; they are an
  operator/programmatic workflow written against the runner (`advanced-api/1-runners-and-clients.md`) and documented
  in `testing/`. This directory is package layout, not a sixth authoring role.

### Constraints

- The package root exports one `pipeline` value.
- The package defines no run entrypoint: no `__main__`, no `run_cli` or `main` function, no CLI, and no code that
  executes a run at import or module scope. The package exports declarations only.
- A package is run by being loaded: the framework's run tooling and the advanced programmatic surface
  (`advanced-api/`) discover the exported `pipeline` value and execute it. The package supplies no inputs and starts
  no run of its own.
- Role directories use the role names above when that role exists.
- A package does not introduce parallel homes for the same role.
- Role directories may contain snake_case modules.
- Each role directory's `__init__.py` re-exports public role classes consumed by other roles.
- Model tools stay in `tools/`.
- Python-owned external calls stay in `integrations/`.

## Application modules hold declarations, not run state

Application modules declare roles, constants, and pure helpers. They do not hold run-affecting mutable state, and
they do not act as run entrypoints or launchers. A
module-level cache, accumulator, counter, registry, or singleton that carries information from one task execution
to another is not part of the application contract, even if a single-process run would tolerate it. State that must
survive is a document; configuration that varies per run is `RunConfig`; process-local infrastructure is owned only
by a service client (`api/9-integrations.md`).

### Constraints

- Application modules do not define mutable module-level globals that hold, accumulate, or carry run state across
  task executions.
- Immutable module-level declarations are allowed: classes, type aliases, constants, and the package-level
  `pipeline` value.
- A helper may read declared constants; it does not read or mutate hidden module state to decide business behavior.
- Process-local mutable infrastructure is not owned at module scope by application code; the only allowed exception
  is the service-client boundary in `api/9-integrations.md`.

## Dependency order points toward orchestration

Package roles depend in one direction. Later roles consume earlier roles; earlier roles do not import later
orchestration to define themselves. Model tools are defined before the prompt contracts that expose them.

### Procedure

1. Define content models, document classes, and document bundles.
2. Define model tools only when prompt contracts need bounded model-callable capabilities.
3. Define prompt contract output models and prompt contracts.
4. Define service clients and polling clients only when a task needs external calls (`api/9-integrations.md`).
5. Define task classes that consume resolved documents and the service clients above, and return durable document
   shapes.
6. Define the `RunConfig` subclass the runner supplies (model selection and run-varying bounds).
7. Define phases that schedule task instances through `PhaseBuilder`.
8. Define the pipeline class that schedules phase instances through `PipelineBuilder`.
9. Construct the pipeline instance in `pipeline.py` as `pipeline = SomePipeline()`.
10. Re-export `pipeline` from the package `__init__.py`.

## Framework validates declarations before work starts

The framework checks declared package shape before task work starts. Declarations with unsatisfied inputs,
incompatible outputs, invalid selectors, unbounded repetition, or undeliverable results fail before
application-authored task code runs. Around the validated application boundaries, the framework owns persistence,
retry, prompt result attachment, and citation attachment.

### Invariants

- A pipeline declaration that cannot run does not start task execution.
- A task that returns an invalid document shape does not publish that shape to later phases.
- A final delivery field that cannot be resolved from available pipeline state fails delivery.

## Standard type checking covers public declarations

Public authoring shapes are designed for standard Python type tools. The framework uses PEP 695-style generics,
protocol-shaped boundaries, and typed selector callables; it does not require a pyright or mypy plugin.

### Constraints

- Application code type-checks against ordinary Python annotations and framework protocol types.
- Planning selectors are pure attribute lambdas over typed document refs or document classes.
- Selector lambdas access durable fields through `.content`.
- The framework rejects selectors containing calls, operators, branches, or non-attribute computation at plan compile
  time.
- Planning-ref attribute access may type as the referenced document content type without framework-specific plugin
  narrowing.
- Phase-input wrappers are subtypes of planning refs (`RequiredInput[T]` is a `DocumentRef[T]`, `OptionalInput[T]`
  is an `OptionalRef[T]`, `AllInputs[T]` is a `CollectionRef[T]`), so passing a wrapper where a ref is expected
  type-checks without a plugin (`api/7-phases.md § Phase inputs are planning refs`).
- Framework validation remains the runtime backstop for declarations that standard type checkers cannot prove.

## A minimal pipeline

The smallest complete package still shows durable state, prompt contract, task, phase, pipeline, run configuration,
input bundle, output bundle, and package export. The framework supplies the pipeline run configuration to the pipeline
builder and resolves planning refs before task execution.

### Examples

#### documents/__init__.py

```python
from enum import StrEnum

from ai_pipeline_core import CitedText, Document, DocumentBundle, Field, FrozenBaseModel


class RiskRating(StrEnum):
    """Closed risk ratings delivered by the review pipeline."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class ReviewRequest(FrozenBaseModel):
    """Request content supplied to the review pipeline."""

    company_name: str = Field(description="Company being reviewed.")
    review_focus: str = Field(description="Risk area the assessment should address.")


class ReviewRequestDocument(Document[ReviewRequest]):
    """Root request document for a company review."""


class ReviewEvidence(FrozenBaseModel):
    """Evidence supplied to the review pipeline."""

    title: str = Field(description="Evidence title.")
    body: str = Field(description="Evidence body.")


class ReviewEvidenceDocument(Document[ReviewEvidence]):
    """Evidence document supplied to the risk assessment."""


class RiskAssessment(FrozenBaseModel):
    """Risk assessment delivered by the review pipeline."""

    rationale: CitedText = Field(description="Evidence-supported reason for the rating.")
    rating: RiskRating = Field(description="Risk rating assigned to the company.")


class RiskAssessmentDocument(Document[RiskAssessment]):
    """Risk assessment document produced by the review task."""


class ReviewInputs(DocumentBundle):
    """Root documents supplied when the run starts."""

    request: ReviewRequestDocument = Field(description="Request document for the review run.")
    evidence: tuple[ReviewEvidenceDocument, ...] = Field(
        description="Evidence documents supplied to the review run.",
    )


class ReviewOutputs(DocumentBundle):
    """Documents delivered after the run completes."""

    assessment: RiskAssessmentDocument = Field(description="Risk assessment delivered by the review run.")
```

#### run_config.py

```python
from ai_pipeline_core import AIModelRef, Field, RunConfig


class ReviewRunConfig(RunConfig):
    """Runner-supplied configuration for the review pipeline."""

    assessment_model: AIModelRef = Field(description="Model capability used for risk assessment.")
```

#### prompt_contracts/__init__.py

```python
from typing import ClassVar

from ai_pipeline_core import Field, PromptContract

from review_app.documents import ReviewEvidenceDocument, ReviewRequestDocument, RiskAssessment


class AssessRiskContract(PromptContract[RiskAssessment]):
    """Assess company risk from the supplied request and evidence documents."""

    purpose: ClassVar[str] = "Assess company risk from the supplied request and evidence documents."
    returns: ClassVar[str] = "A risk assessment with one rating and cited rationale."
    success_criteria: ClassVar[str] = "The response assigns a rating and cites supplied evidence."

    request: ReviewRequestDocument = Field(description="Review request document to assess.")
    evidence: tuple[ReviewEvidenceDocument, ...] = Field(
        description="Evidence documents supporting the risk assessment.",
    )
```

#### tasks/assess_risk.py

```python
from ai_pipeline_core import AIModelRef, Task

from review_app.documents import ReviewEvidenceDocument, ReviewRequestDocument, RiskAssessmentDocument
from review_app.prompt_contracts import AssessRiskContract


class AssessRiskTask(Task):
    """Run the risk assessment prompt and persist its response."""

    model: AIModelRef

    async def run(
        self,
        request: ReviewRequestDocument,
        evidence: tuple[ReviewEvidenceDocument, ...],
    ) -> RiskAssessmentDocument:
        result = await AssessRiskContract(request=request, evidence=evidence).execute(model=self.model)
        return RiskAssessmentDocument.from_prompt_result(result=result)
```

#### phases/assess_risk.py

```python
from ai_pipeline_core import AIModelRef, AllInputs, Phase, PhaseBuilder, PhasePlan, RequiredInput

from review_app.documents import ReviewEvidenceDocument, ReviewRequestDocument
from review_app.tasks.assess_risk import AssessRiskTask


class AssessRiskPhase(Phase):
    """Produce the risk assessment document."""

    model: AIModelRef

    def build(
        self,
        f: PhaseBuilder,
        request: RequiredInput[ReviewRequestDocument],
        evidence: AllInputs[ReviewEvidenceDocument],
    ) -> PhasePlan:
        assessment = f.step(
            AssessRiskTask(model=self.model),
            request=request,
            evidence=evidence,
        )
        return f.outputs(assessment=assessment)
```

#### pipeline.py

```python
from ai_pipeline_core import Pipeline, PipelineBuilder, PipelinePlan

from review_app.documents import ReviewInputs, ReviewOutputs, RiskAssessmentDocument
from review_app.phases.assess_risk import AssessRiskPhase
from review_app.run_config import ReviewRunConfig


class ReviewPipeline(Pipeline[ReviewInputs, ReviewRunConfig, ReviewOutputs]):
    """Plan one company review run."""

    def build(
        self,
        d: PipelineBuilder,
        run_config: ReviewRunConfig,
    ) -> PipelinePlan:
        d.phase(AssessRiskPhase(model=run_config.assessment_model))
        return d.outputs(assessment=d.latest_active(RiskAssessmentDocument))


pipeline = ReviewPipeline()
```

#### __init__.py

```python
from review_app.pipeline import pipeline

__all__ = ("pipeline",)
```
