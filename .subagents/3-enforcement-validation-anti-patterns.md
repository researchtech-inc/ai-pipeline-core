# Enforcement, Validation, and Anti-Pattern Prevention

This document defines the enforcement model for `ai-pipeline-core`: the rules, guards, and validation layers whose purpose is to stop incorrect pipeline code from being written, imported, or executed. The framework’s goal is not merely to detect bad patterns after they appear, but to progressively remove the API surfaces that make those patterns possible and to reject the remainder as early as possible.

The guiding principle is Poka-Yoke: make the correct path the only easy path, and make the wrong path structurally difficult or impossible. In practice, this means layered enforcement across static analysis, import-time validation, build-time plan checks, and runtime guards.

---

## 1. Enforcement Layers

### 1.1 Static analysis
Static analysis catches patterns before code is imported or run. This layer is responsible for broad structural violations, prohibited syntax patterns, bad imports, and obvious type abuses.

The framework relies on:
- `ruff` for style, complexity, and language-level hygiene
- `basedpyright` for strict typing and signature correctness
- `semgrep` for framework-specific forbidden patterns
- `vulture` and `interrogate` for dead code and documentation hygiene where appropriate

### 1.2 Import-time validation
Import-time validation is where the framework’s strongest guarantees live. `__init_subclass__` and related definition-time hooks reject classes that violate framework contracts.

This layer is responsible for:
- invalid task/flow/spec signatures
- invalid document definitions
- illegal file structure
- missing required class-level metadata
- illegal return annotations
- forbidden type patterns in public framework contracts

### 1.3 Build-time validation
Build-time validation happens when deployments and flow plans are assembled. It answers questions that require the full pipeline graph rather than a single class definition.

This layer is responsible for:
- verifying that flow outputs can feed downstream flow inputs
- identifying returned document types that are never consumed
- validating deployment gating rules against real document schemas
- rendering or estimating worst-case execution graphs
- catching structurally unreachable or useless flow outputs

### 1.4 Runtime guards
Runtime guards are the last line of defense. They exist for patterns that cannot be fully prevented statically, such as illegal execution context usage.

This layer is responsible for:
- preventing tasks from calling tasks
- preventing conversations from being created inside flows
- preventing silent data-loss workarounds
- warning when declared bounds are exceeded
- stopping execution when a code path violates a core orchestration rule

---

## 2. Current Hard Guards and Validations

## 2.1 File-level isolation
The framework enforces application file isolation so that a reader can understand a file’s purpose immediately.

Hard rules:
- One `PipelineFlow` per file
- One `PipelineTask` per file
- A file containing a flow must not also contain tasks or standalone specs
- A file containing a task must not also contain flows or standalone specs
- PromptSpec co-location is allowed only under narrow, explicit rules
- Tests, examples, and framework internals are exempt where appropriate

What this prevents:
- mixed orchestration and execution logic in one file
- “god modules” where flow, tasks, specs, helpers, and ad hoc state all drift together
- hidden dependencies that require reading multiple unrelated symbols in the same file

## 2.2 Mandatory docstrings on application classes
Application flows, tasks, and specs must have non-empty docstrings.

What this prevents:
- anonymous orchestration code
- classes with unclear purpose, inputs, or outputs
- AI-generated shells with no useful intent documentation

## 2.3 Module-scope replayable classes
Replayable framework classes must be defined at module scope, not inside functions or closures.

What this prevents:
- `<locals>` qualified names that cannot be encoded or replayed
- broken codec/replay behavior
- dynamically generated types that cannot be re-imported

## 2.4 Task return annotation validation
Task return types are tightly constrained. Historically, bare document returns caused runtime/typing mismatches and were corrected by stronger validation.

The task return contract is enforced at import time:
- returns must be `tuple[DocumentSubclass, ...]`, `None`, list/union forms explicitly supported by the framework, or approved task result shapes
- invalid bare return types are rejected when they do not match runtime normalization semantics
- returns that cannot be serialized, persisted, or reasoned about structurally are rejected

What this prevents:
- hidden tuple wrapping at runtime
- flows accidentally returning nested tuples or non-document items
- type hints that lie about runtime behavior

## 2.5 Document factory path validation
Document creation must go through approved factory methods with provenance semantics.

Static and runtime protections exist for:
- banning direct `Document(...)` construction outside framework internals and tests
- banning bare `Document.create_root(...)`, `Document.derive(...)`, etc.
- ensuring provenance arguments are document objects, not SHA strings
- preventing raw base `Document` instances from existing

What this prevents:
- provenance-free artifacts
- invalid lineage
- “utility documents” that are not real document types
- misuse of the abstract document base as if it were a concrete artifact

## 2.6 Stub / skeleton enforcement
The framework supports explicit stub classes for partially implemented applications, but only under controlled conditions.

The enforcement model includes:
- explicit stub declaration, not heuristic guessing from empty bodies
- deployment-time blocking if stubs remain
- runtime error if stub execution is attempted
- compatibility with import-time validation and semgrep
- registries reset in tests for isolation

What this prevents:
- half-implemented code silently shipping
- placeholder classes being mistaken for production-ready code
- import-time breakage during incremental application development

---

## 3. Static Restrictions on API Shape

## 3.1 No `Any`, `*args`, or `**kwargs` in task/flow contracts
Tasks and flows must have explicit, strongly typed signatures.

Forbidden:
- `Any` in public task/flow/deployment signatures
- `*args`
- `**kwargs`
- `Task.run(**params_dict)`
- any call pattern that hides the real contract from type checking and AST analysis

What this prevents:
- signature vagueness
- “bag of convenience” parameter passing
- silent contract drift
- unreadable orchestration code

## 3.2 No mutable containers in signatures without explicit framework support
Mutable containers are unsafe at orchestration boundaries because they allow hidden mutation and make contracts harder to reason about.

The desired enforcement direction is:
- ban or strongly discourage `list` and `dict` in task/flow signatures
- prefer tuples and frozen models
- if lists/dicts are ever allowed, mutation must still be detectable or structurally blocked

What this prevents:
- tasks mutating shared input state
- hard-to-debug side effects
- contracts that are technically typed but operationally unstable

## 3.3 Limit signature complexity
Very large signatures are a code smell. They indicate one of:
- too much unrelated state crossing the boundary
- missing grouping/composite structures
- over-bundled tasks or flows

The agreed enforcement shape is:
- soft warning around 8 parameters
- hard error around 12
- no silent workaround by collapsing everything into `Any`, dicts, or god-models

What this prevents:
- unreadable `.run()` calls
- giant handoff surfaces
- pseudo-structured but effectively opaque orchestration interfaces

## 3.4 Content model placement and co-location
The framework must stop the “detached models package” anti-pattern.

The enforcement direction is:
- a document’s content model belongs in the same file/module as the document class
- a spec’s raw output model belongs with the spec unless it is literally the document content model
- nested submodels live with their owning parent model
- shared cross-document models are allowed only when genuinely shared, not as a dumping ground

What this prevents:
- 40-model `models/` packages disconnected from document ownership
- wrapper models created only because the framework gives no better shape
- loss of readability when a reader must search multiple files to know what a document contains

## 3.5 List output restrictions
Where structured list outputs are supported, the allowed shapes must be tightly controlled.

Allowed:
- `list[T]` only where `T` is a `BaseModel`
- only in places where the runtime, replay, and structured-output layers fully support it

Rejected:
- `list[str]`
- `list[list[T]]`
- bare `list`
- unsupported nested output shapes

What this prevents:
- “invisible wrapper” schema magic
- provider incompatibilities in strict structured-output mode
- replay/codec ambiguity

---

## 4. Runtime Guards for Illegal Execution

## 4.1 Tasks must not call tasks
A task may orchestrate internal logic, but it may not invoke another task as a subroutine.

Enforcement:
- runtime ContextVar guard in task execution
- task entry checks whether another task is already active
- violation raises immediately

What this prevents:
- hidden execution trees
- spans and retries that no longer reflect real work structure
- “helper task” anti-patterns that turn tasks into mini-flows

## 4.2 Conversations must not be created in flows
LLM calls belong in tasks, not in flows.

Enforcement:
- runtime check in `Conversation` construction or send path
- if current execution context is a flow and not a task, fail

What this prevents:
- untracked LLM work inside orchestration code
- retry/cost/persistence logic bypass
- flows turning into execution engines instead of orchestrators

## 4.3 Flows must not return unchanged inputs
Flows must not pass input documents through unchanged “just in case”.

Enforcement direction:
- compare returned documents against input document identities
- reject or warn when an input document is returned unchanged
- allow only explicitly modified or newly produced documents

What this prevents:
- “return everything” as a lazy default
- deployment-level accumulation of dead artifacts
- downstream flows receiving documents no one actually needs

## 4.4 Fan-out bounds are warnings, not truncation
If a task or flow produces more items than expected, the framework must never silently slice or discard them.

Enforcement direction:
- `max_fan_out` is monitored and warns
- slicing patterns like `[:N]` are forbidden because they cause data loss
- the system processes all items and records the overflow

What this prevents:
- hidden truncation
- AI-generated “quick fixes” that lose data
- false confidence from bounded-looking code that actually cheats

---

## 5. AST-Based Pattern Enforcement

The conversations converged on a key idea: some misuse is best prevented by restricting the legal sublanguage of `run()` methods. The AST parser is not just a linter; it is a structural gate that defines the allowed orchestration grammar.

## 5.1 Allowed orchestration patterns
The allowed patterns inside flow `run()` should be narrow and explicit:

1. Sequential task call
   `await TaskClass.run(...)`

2. Handle dispatch
   `TaskClass.run(...)` without `await`

3. Single-level fan-out comprehension
   `[TaskClass.run(...) for item in items]`

4. Batch collection
   `await collect_tasks(*handles)`

5. Minor conditional step
   `if condition: await TaskClass.run(...)`

6. Serial update loop
   `for item in items: await TaskClass.run(...)`

Everything else becomes suspect.

## 5.2 Banned orchestration shapes
The AST/linter layer should reject:
- nested `def` / `async def` inside `run()`
- module-level async helper functions that call tasks
- helper methods on flows that call tasks
- double/triple nested comprehensions that dispatch tasks
- hidden task dispatch through custom wrappers
- `run_tasks_until(...)` style indirect dispatch where the task call is hidden in argument-group data
- `Task.run(**params_dict)` or other unpacked invocation forms

What this prevents:
- orchestration logic moving out of visible flow code
- accidental mini-DSLs that obscure task order
- code that humans and tools cannot pre-analyze

## 5.3 Why AST limits are not enough on their own
Line limits and simplistic AST bans are weak if they only force code to move around. A 500-line flow can become 20 helper methods on the same class and remain just as bad.

The correct use of AST parsing is:
- to enforce a small number of legal orchestration shapes
- to make execution graphs renderable before execution
- to support debugging and reasoning about worst-case structure
- not to replace design judgment with arbitrary style rules

---

## 6. Return Discipline and Flow-Level Tracking

## 6.1 Return only what is needed next
Tasks and flows should return only documents that are actually needed by downstream steps. Persistence and auditability are framework responsibilities, not excuses for bloated return contracts.

This leads to two tracking levels:

### Level 1: task → flow
Question: does the flow actually use what the task returns?

This is useful for:
- identifying tasks that emit extra review/audit/intermediate documents no caller consumes
- exposing tasks that bundle too much responsibility

But it is difficult to enforce without false positives unless persistence and handoff are cleanly separated.

### Level 2: flow → downstream
Question: does any downstream flow or final result builder consume what this flow returns?

This is the strongest current check because:
- it operates on declared types
- it can be computed at plan/build time
- it directly targets the “flow returns everything” anti-pattern

What this prevents:
- flows forwarding unused documents
- accidental long-lived artifacts with no consumer
- document accumulation that exists only because nobody cleaned the return contract

## 6.2 Terminal consumers and control documents
Some documents are not consumed by downstream flows but are still legitimate:
- final deliverables used by `build_result()`
- loop-control documents used by deployment gating
- explicit terminal artifacts

Any return tracking system must support these as first-class consumers so that legitimate outputs are not mislabeled as waste.

---

## 7. Anti-Pattern Catalog

This section lists the specific patterns the framework should ban or strongly discourage.

## A1. God-flows
A single flow orchestrates many distinct phases, mixes planning, execution, reconciliation, summarization, and output shaping.

Why it is wrong:
- impossible to understand in one read
- too many control paths
- encourages helper-method orchestration and hidden state
- makes task boundaries meaningless

## A2. Helper methods on flows that call tasks
Example shape:
- `flow._gather_evidence()`
- `flow._resolve_conflicts()`

Why it is wrong:
- orchestration escapes the visible `run()` method
- AST analysis becomes unreliable
- task order is harder to review

Pure data-transformation helpers are fine. Helpers that call tasks are not.

## A3. Nested closures inside `run()`
Example shape:
- `async def _one(item): ...`
- `def build_jobs(...): ...` inside a flow

Why it is wrong:
- hides execution edges
- breaks trace readability
- encourages mini-orchestrators inside flows

## A4. Task calling task
A task that uses another task as a helper.

Why it is wrong:
- hides spans
- bypasses flow-level orchestration
- makes retries and persistence unintuitive

## A5. Conversation inside flow
A flow directly constructs and sends a conversation.

Why it is wrong:
- moves execution into orchestration
- makes flow logic impure
- breaks the “tasks do work, flows orchestrate” contract

## A6. Returning everything
A flow returns every document it created, every evidence artifact, every review note, every intermediate result.

Why it is wrong:
- forces downstream filtering
- bloats accumulated state
- encourages huge return unions
- makes it unclear what the actual handoff contract is

## A7. Returning inputs unchanged
A task or flow returns documents it received, unchanged, to “keep them around”.

Why it is wrong:
- turns returns into pass-through cargo
- obscures what was actually produced
- pollutes downstream steps

## A8. Heterogeneous tuple bags
Returns or inputs like:
- `tuple[A | B | C | D, ...]`
- mixed tuples where meaning depends on runtime fishing

Why it is wrong:
- contracts become unreadable
- `[0]` indexing proliferates
- consumers resort to `find_latest`, `isinstance`, and ad hoc filtering

## A9. `find_latest` / `find_all` fishing everywhere
Flows repeatedly extract values from a large document bag.

Why it is wrong:
- every flow starts with plumbing
- signatures lie about actual needs
- code readers cannot see dependencies from the signature alone

## A10. Shuttle / pointer documents
Documents that exist only to move IDs, names, SHAs, or references around.

Why it is wrong:
- they are transport hacks, not domain artifacts
- they multiply document counts without adding semantic clarity
- they usually signal missing grouping or wrong task boundaries

## A11. Fake documents for prompt scaffolding
Creating a document solely to wrap raw text or definitions into prompt context.

Why it is wrong:
- abuses provenance semantics
- pollutes persistence
- indicates the framework lacks or hides the correct prompt-material mechanism

## A12. `cast(Any, ...).derive()` and similar bypasses
Type erasure used to smuggle invalid document creation or invalid content through the type system.

Why it is wrong:
- defeats the entire static contract
- is exactly the kind of AI-generated hack the framework must prohibit

## A13. Monkey-patching `input_document_types`
Changing framework metadata after class definition.

Why it is wrong:
- invalidates type-chain validation
- turns static guarantees into lies
- makes code behavior depend on import order and side effects

## A14. `run_tasks_until` / argument-group calling
Pattern:
- prepare positional/keyword tuples
- pass them to a helper that invokes tasks indirectly

Why it is wrong:
- hides real task calls from AST analysis
- encourages `dict`-driven calling
- makes flows harder to read than explicit `TaskClass.run(...)`

## A15. Double/triple task-dispatch comprehensions
Nested comprehensions that both transform data and launch tasks.

Why it is wrong:
- compress too much logic into one expression
- hide intermediate semantics
- are difficult to debug, review, and bound

## A16. Silent truncation
Patterns like `items[:5]` to satisfy a bound.

Why it is wrong:
- causes data loss
- turns limits into lies
- is a common AI-generated shortcut that must be explicitly banned

## A17. Detached `models/` dumping ground
Content models live in a global package instead of next to the document or spec they belong to.

Why it is wrong:
- destroys locality of meaning
- makes document/spec ownership unclear
- accelerates wrapper-model proliferation

## A18. Reimplementing framework primitives in application code
Examples:
- homegrown provider base classes
- local `SpecTask` clones with erased typing
- fake task orchestration helpers
- app-side provider outcome types duplicating framework facilities

Why it is wrong:
- applications drift from the framework
- identical bugs get solved repeatedly
- end users lose the benefit of a single correct way

---

## 8. Additional Enforcement Mechanisms Identified as Valuable

These were repeatedly identified as worthwhile because they prevent misuse categories that current APIs make too easy.

## 8.1 Semgrep bans for explicit hacks
Good candidates:
- `cast(Any, ...).derive(...)`
- `Task.run(**something)`
- monkey-patching `input_document_types`
- importing from barrel spec modules such as `_common.py`
- bare `Document.create_*()` calls

## 8.2 Better error messages
When rejecting code, the framework should explain:
- what pattern was illegal
- what the author was probably trying to do
- what the correct replacement pattern is

This matters especially for:
- illegal task signatures
- returning input documents
- fake documents used as prompt material
- task-in-task and flow-in-conversation violations
- unsupported return shapes

## 8.3 Build-time renderable execution graph
If `run()` methods are restricted to legal orchestration shapes, the framework can extract:
- which task classes a flow may call
- where fan-out occurs
- what the worst-case execution shape is
- what documents each step can emit

This is enforcement because it forces flows into analyzable structure and makes hidden orchestration much harder.

---

## 9. What Was Considered But Rejected

These ideas came up repeatedly and were rejected because they add complexity without solving the root problem well.

### 9.1 Full declarative FlowSpec / DAG DSL
Rejected because:
- Python already expresses sequence/map/reduce/optional clearly
- real business logic contains serial loops and conditional steps that become worse in a DSL
- it solves the wrong problem by replacing readable code with a mini-language

### 9.2 Auto-collect document persistence as the primary fix
Rejected after detailed analysis because:
- very few real documents were created and not returned
- most return bloat was caused by poor task/flow design, not by persistence coupling
- the explicit “return it if it matters” model is simpler and more predictable

### 9.3 PromptMaterial as a new core primitive
Rejected because existing PromptSpec field types (`MultiLineField`, `StructuredField`, etc.) already cover the real need. The problem was misuse and missing guidance, not missing capability.

### 9.4 `map_tasks` as a must-have primitive
Rejected as non-essential because the framework already supports:
- `Task.run(...)` without await returning handles
- `collect_tasks`
- `as_task_completed`

The real issue was not lack of parallel primitives, but misuse and unfamiliarity.

### 9.5 `DocumentGroup` as a guaranteed first-class primitive
Considered useful, but not universally required. Composite documents and disciplined flow return contracts solve much of the same problem. If introduced, it must justify its cross-cutting complexity.

---

## 10. Final Enforcement Philosophy

The strongest conclusion across the analysis was this:

Bad pipeline code is not primarily the result of missing features. It is the result of APIs and examples that still allow vague, bag-based, helper-heavy orchestration to feel “normal.” The framework should stop trying to rescue bad shapes with warnings after the fact and instead:

- narrow the legal shapes of task and flow code
- make signatures explicit and analyzable
- forbid hidden orchestration
- track only meaningful handoffs
- keep persistence automatic and invisible to application authors
- reject data-loss shortcuts
- provide exactly enough helper surface to make the clean pattern shorter than the hack

The goal is not simply to lint bad code. The goal is to make writing bad code feel unnatural.
