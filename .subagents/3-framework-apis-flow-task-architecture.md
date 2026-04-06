# Framework APIs, Flow/Task Architecture, And Code Organization

This document describes how to build correctly on top of `ai-pipeline-core` from Python. It covers Deployments, Flows, Tasks, Documents, handoff documents, input/output contracts, type rules, file layout, and module boundaries. The goal is to make applications structurally correct, framework-compliant, traceable, and maintainable without leaking business logic into the wrong layer.

---

## 1. Core Architectural Rules

### Deployment owns branching
A `PipelineDeployment` decides the maximum execution graph up front. `build_plan()` should construct every flow that could run based only on options known before execution. Runtime decisions belong in `FieldGate` and `group_stop_if`, driven by small typed control documents emitted by prior flows.

Why:
- The full possible execution path is visible before the run starts.
- Runtime branching remains explicit and inspectable.
- Flow logic stays linear instead of turning into a nested state machine.

### Flows own coarse orchestration, not business mechanics
A flow should express one business purpose and one stable sequence of task invocations. It should not directly perform provider I/O, build documents inline, mutate large shared state bags, or hide most of its logic in large helper methods.

Why:
- Flows are easiest to reason about when they read top-to-bottom like a table of contents.
- Provider calls, retries, cost tracking, and tracing belong in tasks so the framework can observe them properly.
- If orchestration becomes unreadable, the fix is usually to move work into tasks or composite handoff documents, not to cram more logic into the flow.

### Tasks own work and document creation
A task should perform one unit of business work and return the documents it creates. If something deserves tracing, retries, timeout, cost attribution, or provenance, it should be inside a task.

Why:
- The framework persists returned documents automatically.
- Task boundaries give you execution spans, retries, and visibility.
- Inline work in flows becomes invisible plumbing and is hard to audit.

### Documents are the only state that matters across steps
If one part of the pipeline needs information from another part, that information should travel as a `Document[...]`. Avoid hidden in-memory state, synthetic shadow IDs, or cross-step Python bags unless they are purely local and ephemeral.

Why:
- Documents give you explicit provenance.
- Documents survive retries and restarts.
- Documents are what downstream tasks and flows can consume safely.

---

## 2. Deployments

## 2.1 `build_plan()`: Pre-build the maximum path

`build_plan()` should use only deployment options and static mode selection. It should not inspect prior outputs or guess how the run will evolve.

### Correct pattern

```python
class ExampleDeployment(PipelineDeployment[ExampleOptions, ExampleResult]):
    def build_plan(self, options: ExampleOptions) -> DeploymentPlan:
        steps: list[FlowStep] = [FlowStep(PlanningFlow())]
        for round_number in range(1, options.max_loops + 1):
            steps.extend([
                FlowStep(EvidenceGatheringFlow(round_number=round_number), group="loop"),
                FlowStep(AnalysisFlow(round_number=round_number), group="loop"),
                FlowStep(FinalizationFlow(round_number=round_number), group="loop"),
            ])
        steps.append(FlowStep(SynthesisFlow()))
        return DeploymentPlan(steps=tuple(steps))
```

### What belongs here
- Single-round vs multi-round mode
- Prebuilt-plan vs autonomous mode
- Maximum loop count
- Companion-only vs full report mode
- First-round-only or terminal-only flow placement

### What does not belong here
- “Should continue?” decisions
- Reconciliation mode switches from prior evidence
- “Skip because no correction is needed”
- Any decision that depends on documents created at runtime

---

## 2.2 `FieldGate` and `group_stop_if`: Runtime gating via control documents

Runtime branching should be driven by one or more small typed control documents. Do not reverse-engineer broad state to decide whether to skip a flow.

### Correct pattern

```python
class LoopDecisionModel(BaseModel):
    model_config = ConfigDict(frozen=True)
    should_continue: bool
    mode: str
    stop_reason: str


class LoopDecisionDocument(Document[LoopDecisionModel]):
    """Returned by the round-finalization flow."""


def build_plan(self, options: ExampleOptions) -> DeploymentPlan:
    return DeploymentPlan(
        steps=(
            FlowStep(PlanningFlow()),
            FlowStep(ResearchRoundFlow(round_number=1), group="loop"),
            FlowStep(ResearchRoundFlow(round_number=2), group="loop"),
            FlowStep(SynthesisFlow()),
        ),
        group_stop_if={
            "loop": FieldGate(LoopDecisionDocument, "should_continue", op="falsy", on_missing="run"),
        },
    )
```

### Why this matters
- Branch points stay explicit and typed.
- The deployment reads a narrow artifact instead of inspecting many documents.
- It prevents hidden coupling between flows.

---

## 2.3 `build_result()`: Build the final output from final documents only

The deployment result should be assembled from a small final set of output documents, usually one report document and one companion/JSON document.

### Correct pattern

```python
@staticmethod
def build_result(run_id: str, documents: tuple[Document, ...], options: ExampleOptions) -> ExampleResult:
    outputs = FlowOutputs(documents)
    report = outputs.latest(FinalReportDocument)
    companion = outputs.latest(JsonCompanionDocument)
    return ExampleResult(
        success=report is not None,
        report_markdown=report.text if report is not None else None,
        json_companion=companion.text if companion is not None else None,
    )
```

Do not make `build_result()` reconstruct internal intermediate state. That state should already be encoded in final documents.

---

## 3. Flows

## 3.1 One flow, one purpose, one stable sequence

A flow should represent one coherent business phase with a stable task order. If the sequence changes materially based on mode, round number, or runtime state, split into separate flows and let the deployment chain them.

Good examples:
- `PlanningFlow`
- `ResearchRoundFlow`
- `SynthesisFlow`

Bad examples:
- One “god flow” that does planning, gathering, analysis, reconciliation, and synthesis with many mode switches
- One flow that behaves differently for round 1, terminal round, and final synthesis via many `if` branches

---

## 3.2 Flows are the great filter

Tasks return everything they create. Flows return only what the next flow needs.

This is one of the most important structural rules.

### Correct
A research-round flow may create:
- task group docs
- strategy docs
- provider request docs
- provider result docs
- evidence docs
- finding groups
- micro reports
- group reports
- conflict/entity/verification outputs
- loop summary
- refreshed identity memo
- provenance pool
- discovery signals

But it should return only the artifacts required by later flows, for example:
- `EvidenceDocument`
- `GroupReportDocument`
- `ConflictResolutionDocument`
- `EntityResolutionDocument`
- `VerificationNoteDocument`
- `LoopSummaryDocument`
- `IdentityMemoDocument`
- `ProvenancePoolDocument`
- `RoundDiscoverySignalsDocument`

Everything else should still be persisted by the tasks that created it, but should be dropped from the flow output.

### Why
- Keeps downstream flow signatures narrow
- Prevents accumulator blow-up
- Makes cross-flow dependencies explicit

---

## 3.3 Prefer composite handoff documents for cross-flow state

When multiple artifacts always travel together, bundle them into one composite document instead of forwarding ten separate types.

### Example

```python
class PlanningContextModel(BaseModel):
    model_config = ConfigDict(frozen=True)
    contract: ResearchContractModel
    definitions_text: str
    questions: CoverageQuestionsModel
    policy: DecisionPolicyModel
    acquisition_plan: EvidenceAcquisitionPlanModel


class PlanningContextDocument(Document[PlanningContextModel]):
    """Phase 1 outputs bundled for downstream flows."""
```

### When to use a composite document
- The artifacts share a lifecycle
- Downstream always consumes them together
- You are fighting wide flow signatures
- You are tempted to monkey-patch `input_document_types`

### When not to use one
- The bundle would mix unrelated concerns
- Different downstream tasks need different subsets and independent provenance
- The composite document becomes a disguised global state object

---

## 3.4 Control documents are special-case composites

A small control document used for branching is a special kind of handoff document. It should be tiny, typed, and have one clear responsibility.

Examples:
- `LoopDecisionDocument`
- `ReconciliationModeDocument`
- `CorrectionDecisionDocument`

Do not overload broader state documents to drive `FieldGate` or `group_stop_if`.

---

## 3.5 Avoid large helper methods on flows

If a flow requires many large helper methods to stay readable, that is usually a signal that tasks are not owning enough work.

### Bad smell
- Helper methods performing provider submission, result matching, document construction, failure accounting, and context routing
- A mutable `_RoundContext` or giant state bag threaded through helpers
- Nested closures inside flow methods to build issue docs or resolve routing

### Preferred alternatives
- Move business work into a real `PipelineTask`
- Use a pure module-level function only for deterministic data routing
- Use a composite handoff document to avoid repeated extraction logic
- Split into another flow only if the business sequence truly differs

A flow may still use small pure module functions, but its `run()` should remain legible as orchestration rather than business execution.

---

## 3.6 Avoid direct provider I/O in flows

Do not call providers directly from a flow. If a provider call matters, wrap it in a task.

### Bad
```python
await fetch_provider.fetch(url, wait=0)
await deep_research_provider.research(query, wait=0)
```

### Good
```python
result = await ProviderGatewaySubmissionTask.run((request_doc,))
```

### Why
- Provider calls need spans
- Provider calls need retry/timeout/cost tracking
- Provider calls need consistent document outputs
- Flow-level direct calls bypass framework mechanics

---

## 4. Tasks

## 4.1 Use real `PipelineTask` subclasses, not task facades around private framework internals

Do not build custom task frameworks on top of private ai-pipeline-core APIs. Avoid patterns like:
- importing private `_TaskRunSpec`
- a generic `SpecTask` base with `**kwargs: Any`
- helper wrappers that erase types (`map_tasks`, `Sequence[Any]`, `dict[str, Any]` argument packs)

Instead, define explicit `PipelineTask` subclasses with typed `run()` signatures.

### Correct pattern

```python
class GroupSynthesisTask(PipelineTask):
    """Synthesize micro-reports into one group report."""

    reasoning_effort: ClassVar[str] = "high"

    @classmethod
    async def run(
        cls,
        documents: tuple[FindingGroupsDocument | MicroReportDocument | TaskGroupCardDocument | ResearchPlanDocument, ...],
        group_id: str,
        round_number: int,
        model: str,
    ) -> tuple[GroupReportDocument]: ...
```

### Why
- v0.19 file/type validation expects explicit framework usage
- typed signatures produce correct `input_document_types`
- you eliminate private API coupling and `type: ignore` creep

---

## 4.2 Task signatures must be honest and precise

### Inputs
A task’s `documents` parameter should list the exact document types it can accept. Do not use:
- bare `Document`
- `Any`
- giant “accept everything” unions unless they are truly necessary and justified
- post-hoc mutation of `input_document_types` to cover up bad signatures

### Outputs
A task’s return annotation must match what it actually returns. Prefer:

```python
-> tuple[OutputDocument]
-> tuple[DocA | DocB, ...]
```

Avoid:
- returning a bare single document
- fixed-length tuples when the real output is variable
- lying in type annotations and patching with `# type: ignore`

### Why
- ai-pipeline-core uses type extraction to filter documents
- incorrect signatures lead to runtime document starvation
- type honesty is the only reliable routing contract

---

## 4.3 Bare `Document` is not acceptable in task signatures

In newer framework versions, bare `Document` is explicitly rejected in task `run()` signatures. Use specific document subclasses or a justified explicit union.

If the required union becomes too wide, that is a signal to redesign handoff documents, not to weaken typing.

---

## 4.4 Tasks should own document creation

If a task conceptually creates a document, the document should be built inside the task and returned from it.

Bad:
- augmentation request docs created inline in the flow
- provenance pool or discovery signals created inline in the flow
- report assembly metadata created inline in the flow when it conceptually belongs to a task

Good:
- `AugmentationPlanningTask` returns the augmentation plan document
- `LoopSummaryTask` or a dedicated follow-up task returns round-finalization docs
- `ReportAssemblyTask` or equivalent owns the assembled report doc

This keeps provenance clean and execution visible.

---

## 4.5 Use `send_spec(documents=...)` correctly

If a task is calling `send_spec()`, pass documents through `documents=`. Do not combine explicit `with_context(*docs)` with `send_spec(documents=docs)` unless you are certain the docs are not being injected twice.

The important structural rule is: use the framework’s native document injection path rather than hand-rolling your own context semantics.

---

## 4.6 Prefer `follows=` for multi-turn task families

When a second spec is a direct follow-up to a first spec in the same task family, use the framework’s `follows=` / same-conversation continuation pattern instead of inventing a custom multi-turn protocol.

Good examples:
- categorize → detail
- structured review → conditional repair
- rewrite → metadata extraction, if the continuation model supports it cleanly

But do not force `follows=` across unrelated specs or across multiple incompatible first-step parents. In that case, start a clean second conversation and pass the prior output as a field or document.

---

## 4.7 Do not use nested closures or raw coroutines as a hidden task layer

If work deserves retries, spans, or provenance, it should be a task. Avoid nested async closures inside tasks or flows that effectively become invisible sub-pipelines.

Use:
- explicit task invocations
- framework-native collection patterns
- pure functions only for deterministic local transformations

---

## 5. Parallel Dispatch Patterns

The framework’s parallel APIs can be verbose, but the structural rule is simple: parallel work should still be task-shaped.

## 5.1 Sequential pattern

Use plain `await SomeTask.run(...)` when:
- there is only one invocation
- each invocation depends on the previous one
- the task is cheap and the sequence matters

```python
plan = await BootstrapPlanTask.run((user_request,), model=options.planning_model)
critique = await PlanCriticTask.run((plan,), model=options.analysis_model)
revised = await PlanRevisionTask.run((plan, critique), model=options.analysis_model)
```

---

## 5.2 Parallel pattern with handles

If you need fan-out, create many normal task invocations and gather them. Keep the call sites readable.

A clean shape is:

```python
handles = []
for task_card in task_cards:
    handles.append(
        StrategyGenerationTask.run(
            (task_card, plan_doc),
            group_id=group_id_from_card(task_card),
            model=options.analysis_model,
        )
    )
results = await collect_tasks(*handles, deadline_seconds=300.0)
```

or a minimal local helper:

```python
async def parallel[T](coros: Iterable[Awaitable[tuple[T, ...]]]) -> tuple[T, ...]:
    return tuple(doc for result in await asyncio.gather(*list(coros)) for doc in result)
```

The exact wrapper can vary, but the important points are:
- call `Task.run()` normally
- keep everything strongly typed
- do not invent `kwargs` blobs or tuple-of-tuples dispatch structures unless absolutely required
- avoid naked `asyncio.gather()` if you need framework-specific deadlines or partial-completion semantics

---

## 5.3 Do not rely on zip-alignment after lossy gather
If a batch gather can drop failed results, never zip its outputs back against the original inputs by position. Use indexed collection or stable identifiers.

This is a recurring source of silent corruption:
- wrong review paired with wrong draft
- wrong result matched to wrong request
- wrong evidence assigned to wrong section

The fix is always the same:
- preserve indices, or
- preserve task-local stable IDs, or
- gather into keyed structures

---

## 6. Documents And Provenance

## 6.1 Use the framework’s provenance semantics correctly

### `create_root(...)`
Use for inputs and truly root artifacts.

### `derive(...)`
Use when a document is a transformation or interpretation of prior documents.

### `create(...)`
Use when a document is causally triggered by other documents, not derived in content.

### `create_external(...)`
Use for externally sourced content such as fetched pages or provider payloads.

---

## 6.2 `derived_from` vs `triggered_by`

This boundary matters.

### Use `derived_from` when:
- the content is materially built from the parent
- the child is a transformed or interpreted version of the parent
- you want later provenance traversal to show semantic lineage

### Use `triggered_by` when:
- the parent caused the child to exist
- the child is a control or side-effect artifact
- the parent should not be considered semantic input to the content

Examples:
- A rewritten evidence markdown document is `derived_from` the raw provider result
- A control document emitted because a cross-scan found an issue may be `triggered_by` the issue doc
- A safe provider-facing view used only for context should not become phantom `derived_from` provenance if it is not persisted

---

## 6.3 Do not create phantom provenance
If you create transient safe/filtered/helper documents only for prompt context, do not reference them in `derived_from` unless they are real persisted documents.

This leads to:
- provenance graphs pointing to nonexistent SHAs
- broken audit trails
- fake lineage edges

Instead:
- persist them properly, or
- keep them transient and reference the original documents in provenance

---

## 6.4 Do not invent parallel ID systems unless the framework ID is insufficient
If the framework already gives every document a unique ID, do not introduce a second synthetic identifier system unless it serves a distinct and necessary role.

Parallel ID systems cause:
- citation mapping complexity
- regex-based normalization hacks
- duplicated metadata and fragile alias resolution

Use the framework’s canonical document identity for routing and provenance. If you need a shorter human-facing token, derive it as a presentation layer, not as a second routing key.

---

## 6.5 Metadata-only wrapper docs vs content docs
If you need both:
- routing metadata
- full markdown/blob content

do not duplicate full content into multiple documents without a strong reason.

A good pattern is:
- one content-bearing document
- one metadata/routing document that references it

This keeps routing light and prevents token waste.

---

## 6.6 Handoff documents must carry enough to narrow signatures, not hide needed data
A handoff document should simplify flow signatures, but must not force downstream flows to re-look-up missing documents by name that the framework filtered out.

Good handoff:
- includes the exact data downstream needs, or
- narrows the external interface while the flow still receives all required document types honestly

Bad handoff:
- stores only names of documents that are no longer in the flow’s input set
- requires the flow to reconstruct missing state from filtered-out docs
- forces post-hoc type mutations to work around the design

---

## 7. PromptSpec Organization And Structural Constraints

This section is about framework structure, not prompt wording.

## 7.1 One PromptSpec per file
With newer framework rules, keep one primary `PromptSpec` per file. If a follow-up spec uses `follows=` and clearly belongs to the same task family, co-location may be acceptable, but avoid inheritance between specs.

### Why
- v0.19 enforces file-level isolation
- spec inheritance is restricted
- per-file isolation keeps replay and validation sane

---

## 7.2 PromptSpecs must inherit directly from `PromptSpec[...]`
Do not dynamically create spec subclasses that inherit from another spec subclass. The framework validates the base class chain and rejects multi-level PromptSpec inheritance.

If you need shared content:
- use shared roles/rules/constants from a common module
- use mixins that are not PromptSpec subclasses
- duplicate small structural pieces explicitly if necessary

---

## 7.3 `input_documents` must match reality
If a spec declares an input document type, the corresponding task must actually pass those documents. Do not over-declare or under-declare.

Over-declaration causes:
- misleading framework warnings
- false assumptions in prompt design

Under-declaration causes:
- hidden context coupling
- missing rendering/indexing semantics

---

## 7.4 Structured output models must be sequenced correctly
When a model has both decomposition fields and decision fields, place decomposition first and the final decision last.

Examples:
- issues before `needs_repair`
- evidence breakdown before `should_continue`
- assessment details before `answered`

This is both a prompting concern and a framework contract concern because the structured output model itself encodes the sequence.

---

## 8. File And Module Organization

## 8.1 Recommended package layout

A clean app built on `ai-pipeline-core` should look like this:

```text
deep_research/
├── __init__.py
├── __main__.py
├── deployment.py
├── settings.py
├── options.py
├── result.py
├── documents.py
├── models.py
├── prompting/
│   ├── __init__.py
│   └── components.py
├── integrations/
│   ├── __init__.py
│   ├── gateway_types.py
│   └── provider_gateway.py
├── flows/
│   ├── __init__.py
│   ├── planning/
│   ├── research_round/
│   └── synthesis/
├── tasks/
│   └── ...
├── specs/
│   └── ...
└── scripts/
    └── ...
```

### Why
- Deployment and app-wide settings stay easy to find
- Flow/task/spec isolation scales cleanly
- Integrations stay separate from business logic
- Prompting components (roles/rules) remain reusable but non-invasive

---

## 8.2 One flow per file, one task per file, one spec per file
This is the default target. If you need shared helpers, put them in `_helpers.py` or `_common.py` modules that are clearly non-public.

Why:
- file-level isolation in the framework
- easier replay and import validation
- lower coupling and fewer giant modules

---

## 8.3 Public modules define `__all__`
If a module is public, define `__all__`. If it is internal-only, underscore-prefix it.

Why:
- keeps import surfaces explicit
- helps documentation generation
- prevents accidental dependency creep

---

## 8.4 Keep module-scoped replayable classes at module scope
Do not define `Document`, `PipelineTask`, `PipelineFlow`, `PromptSpec`, or related models inside functions. Function-local classes break replay and serialization because of `<locals>` in qualified names.

---

## 8.5 Avoid `__future__` imports unless absolutely necessary
In Python 3.14 codebases that already rely on modern typing, these imports are often unnecessary noise. Prefer the current language directly unless there is a concrete reason.

---

## 9. Common Anti-Patterns And Their Replacements

## 9.1 Anti-pattern: God-flow with many helper methods
**Replace with:** tasks that own work + pure routing helpers +, only if needed, more flows where business purpose diverges.

---

## 9.2 Anti-pattern: Returning every intermediate artifact from a flow
**Replace with:** task persistence + flow-level great filter + composite handoff docs.

---

## 9.3 Anti-pattern: Private task framework wrappers
**Replace with:** explicit `PipelineTask` subclasses with typed signatures.

---

## 9.4 Anti-pattern: Post-class mutation of `input_document_types`
**Replace with:** honest task/flow signatures or properly designed handoff documents. If a temporary workaround is unavoidable, isolate it and document it clearly as a framework limitation, not a normal pattern.

---

## 9.5 Anti-pattern: Direct provider or LLM calls in flows
**Replace with:** tasks.

---

## 9.6 Anti-pattern: Regex/filename conventions as primary routing keys
**Replace with:** framework document IDs, typed references, or stable structured fields.

---

## 9.7 Anti-pattern: Hidden state bag (`_RoundContext`, mutable dicts spread everywhere)
**Replace with:** task outputs, handoff docs, and pure functions that return values.

---

## 9.8 Anti-pattern: Bare `Document`, `Any`, `**kwargs` in task interfaces
**Replace with:** precise unions and explicit keyword parameters.

---

## 10. Practical Build Rules

If you are building a new application on `ai-pipeline-core`, follow these rules:

1. Start with the deployment and define the maximum flow chain.
2. Identify runtime branch points and model each with a tiny control document.
3. Define the minimum durable document types needed across flow boundaries.
4. Bundle always-together cross-flow artifacts into composite handoff documents.
5. Make each task a real `PipelineTask` with honest input and output types.
6. Keep flows readable by pushing work into tasks, not into helper-method labyrinths.
7. Return only downstream-needed docs from flows.
8. Use framework-native conversation/spec patterns rather than inventing wrappers.
9. Preserve provenance correctly with `derived_from` and `triggered_by`.
10. Treat every widening of signatures or handoff payloads as a design smell to be examined.

---

## 11. A Good Standard To Aim For

A well-structured `ai-pipeline-core` app should have:

- one deployment that is easy to inspect
- a small set of flows with stable purposes
- tasks that own real work and return real documents
- narrow flow signatures
- no hidden provider or LLM calls in flows
- no private-framework scaffolding
- explicit provenance
- no giant mutable orchestration bag
- no post-hoc type patching as normal practice
- code that reads like a workflow, not like a rescue mission

That is what “using ai-pipeline-core correctly” means in practice.
