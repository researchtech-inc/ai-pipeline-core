# Core Authoring Model: Documents, Tasks, Flows, Deployments, Specs, Providers

This document defines how applications are meant to be authored on top of `ai-pipeline-core`. It focuses on the primary abstractions, their contracts, and the intended coding style for building pipelines that are readable, strongly typed, modular, and easy to debug.

The core idea is simple:

- **Documents** carry durable business artifacts and typed state.
- **Tasks** perform atomic units of work and produce documents.
- **Flows** orchestrate tasks and decide what moves forward.
- **Deployments** build the full flow plan ahead of time and gate it deterministically.
- **PromptSpecs** define LLM interactions.
- **Providers** integrate with external systems through typed, stateless APIs.

The framework is supposed to absorb tracing, persistence, retry, logging, and execution tracking so application code can stay close to business logic.

---

## 1. Mental Model

A pipeline application has four layers:

1. **Documents**  
   Durable artifacts with provenance. They represent the business state of the system.

2. **Tasks**  
   Atomic workers. A task takes typed inputs, performs one unit of work, and returns typed outputs.

3. **Flows**  
   Orchestrators. A flow calls tasks in order, branches when needed, and returns only the artifacts the next flow actually needs.

4. **Deployment**  
   The top-level plan. The deployment determines all possible flows for the run ahead of time and decides, at runtime, which of those pre-planned flows are actually executed.

That split matters:

- If logic is about **doing work**, it belongs in a **task**.
- If logic is about **sequencing tasks**, it belongs in a **flow**.
- If logic is about **which flows can run, and in what overall order**, it belongs in the **deployment**.
- If data needs to survive beyond one local calculation, it becomes a **document**.

---

## 2. Project Structure

A pipeline repository should be organized around flows, not around implementation technique.

A good structure looks like:

```python
my_pipeline/
    deployment.py
    options.py
    result.py
    providers/
        __init__.py
        fetch.py
        search.py
    flows/
        planning/
            planning.py
            documents.py
            tasks/
                analyze_request.py
                write_plan.py
                write_plan_spec.py
        research_round/
            research_round.py
            documents.py
            tasks/
                gather_evidence.py
                gather_evidence_spec.py
                resolve_conflicts.py
                resolve_conflicts_spec.py
        synthesis/
            synthesis.py
            documents.py
            tasks/
                write_report.py
                write_report_spec.py
```

### Structure rules

- Each **flow** lives in its own module under `flows/{flow_name}/`.
- Tasks that belong to that flow live in `flows/{flow_name}/tasks/`.
- Prompt specs should live next to the task that uses them, usually as sibling files like `task_name.py` and `task_name_spec.py`.
- Document content models should live in the same file as the document class that owns them.
- Shared tasks or shared specs may live in top-level `tasks/` or `specs/` only when they are truly cross-flow and not owned by one flow.

### Why this structure

This is not just about neatness. It makes the code navigable by intent:

- To understand a flow, you open one directory.
- To understand what a task consumes and produces, you open its task file and its document file.
- To understand an LLM interaction, you open the task file and the nearby spec file.
- You do not need to hunt through a global `models/` package or giant utility modules.

---

## 3. Documents

## 3.1 What a Document is

A `Document` is a durable, typed artifact with provenance. It exists to make pipeline boundaries legible.

A document should answer three questions immediately:

1. **What is this artifact?**
2. **Where did it come from?**
3. **Who can consume it next?**

A document is not just “some data.” It is the named contract between steps.

### Good document examples

- `ResearchPlanDocument`
- `EvidenceDocument`
- `GroupReportDocument`
- `LoopDecisionDocument`
- `IdentityMemoDocument`

### Bad document examples

- `PlanningInputDocument`
- `RoundHandoffDocument`
- `ContextBundleDocument`
- `WrapperDocument`
- `AnySimplifiedDocument`

Those are transport artifacts or “bag of stuff” wrappers, not domain artifacts.

---

## 3.2 Typed document content

Document content should be typed with a concrete content model:

```python
from pydantic import BaseModel, ConfigDict
from ai_pipeline_core import Document


class ResearchPlanModel(BaseModel):
    model_config = ConfigDict(frozen=True)

    company_name: str
    focus_areas: tuple[str, ...]
    excluded_entities: tuple[str, ...]


class ResearchPlanDocument(Document[ResearchPlanModel]):
    """The current research plan for the company under investigation."""
```

### Important principles

- The **content model belongs with the document**.
- If a document carries structured state, that structure must be visible in the same file.
- If a document is plain text or markdown, `Document[str]` is valid.
- A separate global `models/` package for document content is the wrong default. It disconnects the data schema from the artifact that carries it.

### When two models are acceptable

A `PromptSpec` may define its own raw output model if the LLM output is not the same as the final document content. In that case:

- the **raw output model** lives with the spec,
- the **durable content model** lives with the document,
- a task converts one into the other.

That is different from splitting one document’s content model into a detached global `models/` directory.

---

## 3.3 List content

When the natural artifact is a list of typed items, use a typed list directly instead of inventing wrapper models purely to satisfy structured output.

```python
class TaskGroupModel(BaseModel):
    model_config = ConfigDict(frozen=True)

    group_id: str
    description: str


class TaskGroupsDocument(Document[list[TaskGroupModel]]):
    """The current set of task groups for a round."""
```

Likewise, a prompt spec can output a typed list:

```python
from ai_pipeline_core import PromptSpec


class TaskGroupsSpec(PromptSpec[list[TaskGroupModel]]):
    """Group the research agenda into coherent task groups."""

    request: str
```

### Why this matters

Without list support, developers tend to create wrapper models like:

```python
class TaskGroupsModel(BaseModel):
    groups: list[TaskGroupModel]
```

That wrapper often has no business meaning. It exists only because the framework forced it. Over time, that produces model bloat and unreadable `models/` packages.

Use list output when the artifact really is “a list of X.”

---

## 3.4 Composite documents

When several artifacts are always produced, transported, and consumed together across flow boundaries, bundle them into a **composite document**.

```python
class PlanningContextModel(BaseModel):
    model_config = ConfigDict(frozen=True)

    plan: ResearchPlanDocument
    identity: IdentityMemoDocument
    provider_config: ProviderConfigDocument


class PlanningContextDocument(Document[PlanningContextModel]):
    """The durable planning context needed by downstream research flows."""
```

### When composite documents are correct

Use them when the grouped artifacts:

- share the same lifecycle,
- are almost always passed together,
- represent one meaningful handoff concept.

### When they are wrong

Do not use composite documents to hide unrelated artifacts in one container. That recreates the same “bag of docs” problem under a new name.

---

## 3.5 Provenance and construction paths

Documents must be created through the correct factory method so provenance stays meaningful.

### `create_root`
Use when the document originates outside the pipeline and has no upstream document provenance.

```python
request = UserRequestDocument.create_root(
    name="request.md",
    content="Research company X and its leadership.",
    reason="CLI input",
)
```

### `derive`
Use when a document is derived from earlier documents.

```python
plan = ResearchPlanDocument.derive(
    name="plan.json",
    content=plan_model,
    derived_from=(request,),
)
```

### `create`
Use when a document is causally triggered by earlier documents, not necessarily a transformation of them.

### `create_external`
Use when the document comes from an external source like a URL or provider result.

### Why this matters

Provenance is not incidental metadata. It is how the system stays auditable. If a reader needs to know how a report or decision came to exist, provenance should make that obvious.

---

## 3.6 What should not be a document

Do **not** make a document for:

- a temporary prompt scaffold,
- a lookup table,
- a local accumulator,
- a transient grouping helper,
- a short-lived routing or helper structure that never crosses a task or flow boundary.

Use a frozen `BaseModel` or dataclass for transient orchestration state inside flows.

Example:

```python
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class CrossResolutionContext:
    group_reports: tuple["GroupReportDocument", ...]
    evidence: tuple["EvidenceDocument", ...]
    round_number: int
    model: str
```

This is not a document. It is local orchestration state.

---

## 4. PromptSpecs and Conversations

## 4.1 PromptSpec is the LLM contract

A `PromptSpec[T]` defines an LLM interaction that yields a typed result `T`.

```python
from pydantic import BaseModel, ConfigDict
from ai_pipeline_core import PromptSpec


class SourceAnalysisModel(BaseModel):
    model_config = ConfigDict(frozen=True)

    summary: str
    key_claims: tuple[str, ...]
    confidence: str


class SourceAnalysisSpec(PromptSpec[SourceAnalysisModel]):
    """Analyze one source and extract its most important claims."""

    source_text: str
    research_question: str
```

### PromptSpec rules

- The **docstring** is the instruction prompt.
- Short scalar fields become normal prompt parameters.
- Long or structured context should use field types designed for that purpose, not fake documents.
- The output type should be strongly typed and explicit.

---

## 4.2 Prompt fields vs documents

There are two kinds of LLM input:

1. **Prompt fields**  
   Short values or deliberately structured prompt data.

2. **Documents**  
   Durable artifacts added to LLM context because they are part of the pipeline state and should remain auditable.

Use prompt fields for things like:

- a short user request,
- a numeric threshold,
- a mode selector,
- a one-off instruction paragraph.

Use documents when the content is a business artifact that should remain part of the pipeline’s lineage.

### Important implication

Do not create fake documents just to get more context into the prompt. If the content is not a real artifact, it does not belong in a document.

---

## 4.3 Sending specs

The normal task-level pattern is:

```python
from ai_pipeline_core import Conversation


conv = Conversation(model=model)
result = await conv.send_spec(spec, documents=(context_doc_1, context_doc_2))
```

### What `send_spec` returns

- If the spec output is typed, `result` is the parsed output model or typed list.
- If the spec is text-only, you use the conversation text output.

The important contract is: the task should receive a **typed** result from the LLM and then convert it into durable documents.

---

## 4.4 Multi-turn usage

A task may use multiple turns on one `Conversation` when the turns are genuinely part of the same interaction and the second turn should see the history from the first.

That is different from flows calling multiple tasks or tasks orchestrating other tasks.

---

## 4.5 Post-processing

If the LLM output is not yet valid business state, fix it in code **inside the task** before constructing the document.

Typical examples:

- assign stable IDs,
- normalize references,
- validate external consistency,
- compute derived invariants,
- map raw output into a stricter domain model.

The LLM proposes. Code enforces.

---

## 5. Tasks

## 5.1 What a task is

A task is an atomic unit of work.

A task should be readable as:

> “Given these typed inputs, do this one thing, and produce these typed documents.”

A task is not a mini-flow. It should not orchestrate other tasks.

---

## 5.2 Task shape

Tasks are class-based and use a classmethod `run`.

```python
from ai_pipeline_core import PipelineTask


class WritePlanTask(PipelineTask):
    """Create the first research plan from the user request."""

    @classmethod
    async def run(
        cls,
        request: "UserRequestDocument",
        model: str,
    ) -> "ResearchPlanDocument":
        spec = PlanSpec(request=request.text)
        conv = Conversation(model=model)
        result = await conv.send_spec(spec)
        return ResearchPlanDocument.derive(
            name="plan.json",
            content=result,
            derived_from=(request,),
        )
```

### Task rules encoded in the shape

- Inputs are **named** and **typed**.
- There is no generic `documents` bag.
- The task does one thing.
- The return type is explicit.
- The task does real work when called.

---

## 5.3 Allowed return shapes

The discussions converge on three legitimate return shapes:

### A. Single document
Use when the task always produces exactly one document.

```python
async def run(...) -> ResearchPlanDocument:
```

### B. Homogeneous tuple of one document type
Use when the task produces zero or more of the same artifact type.

```python
async def run(...) -> tuple[EvidenceDocument, ...]:
```

### C. Named result object for multiple document outputs
Use when a task genuinely produces multiple different document outputs and the caller must know which is which.

```python
from pydantic import BaseModel, ConfigDict


class SpotCheckResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    report: "SpotCheckReportDocument"
    decision: "RepairDecisionDocument"


class SpotCheckTask(PipelineTask):
    @classmethod
    async def run(...) -> SpotCheckResult:
        ...
```

### Why these shapes

They eliminate the worst patterns:

- no heterogeneous document tuples that require fishing by type,
- no scalar flags detached from the document contract,
- no `[0]` indexing when a task always returns one document.

---

## 5.4 What tasks should not return

A task should not return:

- input documents unchanged,
- audit-only artifacts that nobody uses,
- temporary helpers,
- generic “review” or “wrapper” artifacts that exist only because the task was split poorly.

If a task does **plan → review → improve plan**, and the review is not needed by the next step, then the task should return the final plan only. The review is not part of the handoff contract.

---

## 5.5 Task parameters should be explicit

Do not pass bags of values around. If several arguments always travel together and represent one real concept, use a small frozen model or composite document.

Bad:

```python
await Task.run(
    plan=plan,
    identity=identity,
    prior_reports=prior_reports,
    prior_signals=prior_signals,
    prior_summaries=prior_summaries,
    provenance=provenance,
    model=model,
)
```

Better when those pieces are one real handoff concept:

```python
await Task.run(
    planning=planning_context,
    round_history=round_history,
    model=model,
)
```

### Important limitation

A shared parameter object is valid only when it models one real concept. It must not become a garbage bag of convenience fields.

---

## 5.6 Control flow through documents, not booleans

If a task influences later orchestration, it should usually do so through a **typed control document**, not through a naked scalar return.

Good:

```python
class LoopDecisionModel(BaseModel):
    model_config = ConfigDict(frozen=True)

    should_continue: bool
    terminal_run: bool
    reason: str


class LoopDecisionDocument(Document[LoopDecisionModel]):
    """Control decision for whether another round should run."""
```

Then a task returns `LoopDecisionDocument`, and the flow or deployment inspects `decision.parsed.should_continue`.

That keeps control state visible and auditable.

---

## 6. Flows

## 6.1 What a flow is

A flow is a small piece of orchestration code. It wires tasks together, carries results forward, and decides what to return.

A good flow reads like a recipe:

1. call task A,
2. fan out task B,
3. collect results,
4. maybe run task C,
5. return the handoff artifacts.

It does not hide work in helper methods or task chains.

---

## 6.2 Flow shape

```python
from ai_pipeline_core import PipelineFlow


class PlanningFlow(PipelineFlow):
    """Turn the user request into the initial planning context."""

    async def run(
        self,
        request: "UserRequestDocument",
        options: "ResearchOptions",
    ) -> tuple["PlanningContextDocument", ...]:
        plan = await WritePlanTask.run(request=request, model=options.planning_model)
        identity = await WriteIdentityTask.run(request=request, plan=plan, model=options.analysis_model)
        return (PlanningContextDocument.derive(
            name="planning-context.json",
            content=PlanningContextModel(plan=plan, identity=identity),
            derived_from=(request, plan, identity),
        ),)
```

### Flow characteristics

- `run` is an instance method.
- Inputs are named and typed.
- It only orchestrates tasks.
- It may branch or loop when orchestration genuinely requires it.
- It returns only what the next flow or final result builder needs.

---

## 6.3 The “great filter” rule

Flows are the great filter of pipeline state.

Tasks may produce many artifacts during a flow. The flow should return only the subset needed later.

### Why this rule matters

If flows return everything:

- document sets grow every round,
- later flows receive enormous heterogeneous bags,
- signatures explode,
- readers cannot tell what matters.

A flow should not return:

- documents it received and did not modify,
- artifacts only used inside the same flow,
- audit or debugging artifacts not needed by later flows.

---

## 6.4 Legal orchestration patterns

The discussions repeatedly converged on a small set of acceptable flow patterns.

### 1. Sequential chain

```python
plan = await WritePlanTask.run(request=request, model=options.planning_model)
identity = await WriteIdentityTask.run(plan=plan, model=options.analysis_model)
```

### 2. Fan-out dispatch

```python
handles = [
    AnalyzeSourceTask.run(source=source, plan=plan, model=options.analysis_model)
    for source in sources
]
batch = await collect_tasks(*handles)
```

### 3. Serial loop with state threading

```python
state = initial_state
for issue in issues:
    state = await ApplyIssueTask.run(state=state, issue=issue, model=options.analysis_model)
```

### 4. Small conditional

```python
if spot_check.parsed.requires_repair:
    repaired = await RepairSectionTask.run(section=section, model=options.analysis_model)
```

### 5. Data transformation before dispatch

Use a pure module-level function when the flow needs to reshape data before task dispatch.

```python
request_specs = flatten_execution_plans(execution_plans)
handles = [
    GenerateRequestTask.run(spec=spec, model=options.fast_model)
    for spec in request_specs
]
```

### 6. Parallel groups with separate identities

If multiple task classes share the same context, create their handle lists separately, then collect them.

```python
ctx = CrossResolutionContext(
    reports=reports,
    evidence=evidence,
    round_number=self.round_number,
    model=options.analysis_model,
)

conflict_handles = [ResolveConflictTask.run(issue=issue, context=ctx) for issue in conflicts]
entity_handles = [ResolveEntityTask.run(issue=issue, context=ctx) for issue in entities]
verification_handles = [VerifyClaimTask.run(issue=issue, context=ctx) for issue in verifications]

batch = await collect_tasks(*conflict_handles, *entity_handles, *verification_handles)
```

---

## 6.5 What flows should not do

A flow should not:

- create documents directly,
- call `Conversation` directly,
- call tasks from helper methods,
- hide orchestration inside module-level async functions,
- use `run_tasks_until` or generic `argument_groups` bags,
- forward everything “just in case,”
- return its inputs unchanged,
- use giant heterogeneous document unions as a substitute for proper modeling.

If a flow needs many extraction lines, many filters, or many helper methods, the problem is usually one of:

- missing composite documents,
- wrong flow boundaries,
- task doing too much,
- or the underlying framework resolution model being too bag-oriented.

---

## 6.6 Flow decomposition

A flow should represent one meaningful phase.

For a multi-round research pipeline, a good decomposition is usually:

- `PlanningFlow`
- `ResearchRoundFlow`
- `ReportSynthesisFlow`

If a research round itself is still too large, split it into sub-flows only when the sub-flows are meaningful orchestration phases, not arbitrary code splitting.

Examples of meaningful sub-flows:

- `RoundPlanningFlow`
- `EvidenceAcquisitionFlow`
- `FindingsAnalysisFlow`
- `RoundFinalizationFlow`

Examples of bad splits:

- “first 80 lines of the same logic” flow,
- one-time behavior that should have been moved earlier,
- micro-flows that exist only to satisfy line-count rules.

A flow should be short because its responsibility is narrow, not because its logic was artificially fragmented.

---

## 6.7 Composite handoff documents

When a flow would otherwise accept or return too many separate artifacts, define one meaningful composite handoff document.

Examples:

- `PlanningContextDocument`
- `RoundHistoryDocument`
- `EvidenceStateDocument`

This is the main way to keep flow signatures readable without losing type clarity.

---

## 7. Deployments

## 7.1 What a deployment is

A deployment is the global orchestrator for the run. It owns the complete plan of possible flows.

A deployment should answer:

- what flows may run,
- in what order,
- under what conditions a flow is skipped,
- what types count as terminal outputs.

The deployment should make the run graph visible ahead of time.

---

## 7.2 Deterministic pre-built plan

The preferred shape is:

- build the maximum possible sequence of flows ahead of time,
- skip flows at runtime using typed control artifacts.

That gives a deterministic plan without losing dynamic behavior.

Conceptually:

```python
class DeepResearchDeployment(PipelineDeployment):
    def build_flows(self, options: DeepResearchOptions) -> Sequence[PipelineFlow]:
        flows = [PlanningFlow()]
        for round_number in range(1, options.max_rounds + 1):
            flows.append(ResearchRoundFlow(round_number=round_number))
        flows.append(ReportSynthesisFlow())
        return flows
```

Then runtime control decides which of those flows actually execute.

---

## 7.3 Runtime gating

Runtime gating should be driven by typed control documents produced by earlier flows.

Examples:

- `LoopDecisionDocument` determines whether more rounds run.
- `ReconciliationModeDocument` determines which reconciliation flow variant runs.
- `ViabilityDocument` determines whether a phase is skipped.

The deployment should not guess from arbitrary state. It should read typed artifacts produced by the pipeline itself.

---

## 7.4 Deployment responsibilities

A deployment should do these things and no more:

- build the full possible flow list,
- gate flows based on typed control outputs,
- provide options/configuration,
- assemble the final result from terminal artifacts.

A deployment should not contain business logic that belongs inside a task or flow.

---

## 7.5 Final result

`build_result()` should read the terminal artifacts and assemble the final application result. That is the only place where the deployment needs to care about the final output shape.

---

## 8. Providers

## 8.1 Provider role

Providers connect the application to external services. They should be stateless from the caller’s perspective.

The goal is simple task code:

```python
result = await ai_search_provider.search(query, provider="perplexity", wait=300)
```

The task should not manage client initialization, locks, retries, auth, or polling loops manually.

---

## 8.2 Provider base classes

The framework provides provider infrastructure so apps do not reinvent it.

At a high level:

- `ExternalProvider` handles HTTP client lifecycle, retries, auth errors, and test override support.
- `StatelessPollingProvider[RequestT, ResultT]` handles submit/poll workflows where the service is effectively request-key based and idempotent.

### Key authoring principle

The provider API should feel like a normal method call from task code. The provider object may have internal caching, lazy initialization, and locking, but none of that leaks into task authoring.

---

## 8.3 Provider singletons

Module-level provider instances are acceptable for infrastructure providers when they are caller-stateless.

Example pattern:

```python
# infrastructure singleton
ai_search_provider = AISearchProvider(
    base_url=settings.fetch_service_url,
    api_key=settings.fetch_api_key,
)
```

Tasks then import and use them directly.

Why this is acceptable:

- the state is internal transport state,
- callers do not depend on call ordering,
- test override mechanisms isolate tests cleanly.

---

## 8.4 Stateless polling pattern

A common pattern is:

1. submit early,
2. do other work,
3. later request the same work again with wait,
4. the service resolves to the same logical slot and returns the finished result.

This allows flows to start slow provider work as soon as possible without carrying opaque handles through the pipeline.

A `ProviderRequestDocument` should capture the **semantic request**, not transport-specific internals.

---

## 9. Current Best-Practice Patterns

This section summarizes the highest-value patterns that emerged repeatedly.

### 9.1 Use documents to make boundaries obvious
A reader should understand what a task or flow consumes and produces by looking only at the document types in the signature.

### 9.2 Use composite documents for cross-flow handoff
If five artifacts always travel together, define one meaningful composite handoff document.

### 9.3 Keep tasks atomic
A task should do one coherent piece of work. If it internally performs unrelated stages, split it.

### 9.4 Let flows orchestrate
Conditionals and loops belong in flows when they are orchestration decisions. They should be visible there, not hidden in tasks.

### 9.5 Let deployments own global routing
If a step only exists in round 1, or only in terminal mode, make that a deployment decision, not a hidden flow-level trick.

### 9.6 Return only what moves forward
Tasks and flows should not return everything they happened to touch. Return only what later steps actually need.

### 9.7 Use typed control documents
If later orchestration depends on a decision, materialize that decision as a typed document.

### 9.8 Use provider abstractions directly
Tasks should call providers through typed methods. They should not reimplement polling, batching, or retry semantics themselves.

---

## 10. Example Pipeline Shape

A readable pipeline built with this model often ends up looking like:

```python
class PlanningFlow(PipelineFlow):
    async def run(
        self,
        request: UserRequestDocument,
        options: ResearchOptions,
    ) -> tuple[PlanningContextDocument, ...]:
        plan = await WritePlanTask.run(request=request, model=options.planning_model)
        identity = await WriteIdentityTask.run(request=request, plan=plan, model=options.analysis_model)
        context = PlanningContextDocument.derive(
            name="planning-context.json",
            content=PlanningContextModel(plan=plan, identity=identity),
            derived_from=(request, plan, identity),
        )
        return (context,)


class ResearchRoundFlow(PipelineFlow):
    round_number: int

    async def run(
        self,
        planning: PlanningContextDocument,
        round_history: RoundHistoryDocument | None,
        options: ResearchOptions,
    ) -> tuple[EvidenceStateDocument | RoundHistoryDocument | LoopDecisionDocument, ...]:
        requests = await PlanRequestsTask.run(
            planning=planning,
            round_history=round_history,
            model=options.fast_model,
        )

        handles = [
            GatherEvidenceTask.run(request=request, planning=planning, model=options.analysis_model)
            for request in requests
        ]
        batch = await collect_tasks(*handles)

        state = await ConsolidateEvidenceTask.run(
            planning=planning,
            evidence=batch.completed,
            round_history=round_history,
            model=options.analysis_model,
        )

        decision = await DecideNextRoundTask.run(
            planning=planning,
            state=state,
            round_number=self.round_number,
            model=options.analysis_model,
        )

        new_history = RoundHistoryDocument.derive(
            name=f"round-{self.round_number}-history.json",
            content=RoundHistoryModel(state=state, decision=decision),
            derived_from=(planning, state, decision),
        )

        return (state, new_history, decision)


class ReportSynthesisFlow(PipelineFlow):
    async def run(
        self,
        planning: PlanningContextDocument,
        state: EvidenceStateDocument,
        options: ResearchOptions,
    ) -> tuple[AnalysisReportDocument, ...]:
        report = await WriteReportTask.run(
            planning=planning,
            state=state,
            model=options.analysis_model,
        )
        return (report,)


class DeepResearchDeployment(PipelineDeployment):
    def build_flows(self, options: ResearchOptions) -> Sequence[PipelineFlow]:
        flows: list[PipelineFlow] = [PlanningFlow()]
        for round_number in range(1, options.max_rounds + 1):
            flows.append(ResearchRoundFlow(round_number=round_number))
        flows.append(ReportSynthesisFlow())
        return flows
```

This shape is the core target:

- deployment builds the whole sequence,
- flows are short and orchestration-only,
- tasks do the work,
- documents make boundaries legible,
- providers hide transport details,
- and the code reads like business logic instead of framework plumbing.

---

## 11. Final Standard

A senior engineer should be able to open a flow or task file and answer, immediately:

- What does this step do?
- What does it need?
- What does it produce?
- What happens next?
- Why is this artifact a document?
- Why is this branch or loop here?

If the answer requires jumping through multiple helper files, `find_latest` chains, generic wrappers, or synthetic transport artifacts, the code is not following the authoring model correctly.