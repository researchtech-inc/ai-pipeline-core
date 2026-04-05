# Empirical Evidence: Bugs, Failure Modes, Case Studies, and Decision History

This document records what actually failed in real framework use, what anti-patterns appeared in application code, which framework bugs were uncovered, which redesign ideas were explored, and which conclusions held up after repeated re-analysis. It is the evidence base for future framework work.

The central historical fact is simple: a large application built on an older version of the framework produced an overengineered, brittle, hard-to-read pipeline with excessive document churn, task boilerplate, hidden orchestration, and duplicated infrastructure. Repeated analysis showed that some problems were genuine framework gaps, but many others were misuse of existing primitives, poor guidance, or API shapes that made the wrong thing too easy.

## 1. Primary Case Studies

## 1.1 The ai-diligence final report failure
A real flow failed at runtime because a task annotated as returning a single document was normalized by the framework into a tuple, and the caller treated it as a single document. The flow ended up returning a tuple containing a nested tuple, which tripped runtime validation with “flow returned non-Document items.”

What this proved:
- Import-time validation was too permissive: bare `-> MyDocument` task returns were allowed even though runtime always normalized task outputs to tuples.
- Callers trusted the annotation and were misled by it.
- The framework had a type/runtime contract mismatch.

What changed as a result:
- Bare single-document task return annotations were later rejected.
- The issue became the canonical example of why “developer convenience” annotations that lie about runtime behavior are harmful.

## 1.2 The ai-deepresearch pipeline as the main failure corpus
A large application codebase built on an older framework version became the main empirical source for almost every later proposal. It grew to roughly 5,900 lines, 35+ document types, a separate `models/` package with dozens of disconnected content models, multiple custom provider layers, prompt barrels, monkey-patched metadata, and very large flows with nested orchestration.

This codebase was not merely “messy.” It revealed repeated failure classes:
- Framework primitives were reimplemented locally.
- Existing framework capabilities were ignored or unknown.
- Documents were used as transport bags, prompt wrappers, and orchestration state.
- Flows returned everything they touched, creating huge accumulated document bags.
- Tasks and flows became unreadable because signatures and return types no longer described real intent.

This application became the reference “what went wrong” corpus used in later framework analysis.

## 1.3 The provider layer refactor in a real application
A real application built its own provider infrastructure despite the framework later gaining `ExternalProvider` and `StatelessPollingProvider`. The custom provider layer duplicated polling logic, citation parsing, request/result models, testing helpers, and attachment reconstruction.

This proved two different things:
- The framework did have genuine missing provider abstractions at one stage.
- Once those abstractions existed, applications still needed strong guidance to avoid re-implementing them.

## 2. Framework Bugs Discovered in Real Use

## 2.1 Type contract and validation bugs

### Bare task document returns were accepted but wrong
Symptom:
- Tasks annotated `-> MyDocument` passed import-time validation.
- Runtime normalized the return into `(doc,)`.
- Callers treated the value as a document, not a tuple.

Root cause:
- Validation explicitly allowed single document subclasses in task return annotations.
- Runtime always wrapped document returns into tuples.

Fix:
- Reject bare document return annotations and require tuple returns or another explicit shape.

### `Conversation.parsed` contract problems
Two issues were found:
- The type parameter on `Conversation` was too loose.
- `parsed` could effectively behave as “maybe None,” which weakened downstream guarantees.

Later changes tightened this area by:
- Binding conversation generics to `str | BaseModel`.
- Making the “no parsed output available” path raise rather than silently returning `None`.

### Null `choices` crash in model response building
Symptom:
- Some providers returned `choices=None` or an empty list.
- Response building crashed with `TypeError` while iterating `response.choices`.

Root cause:
- The client assumed the provider always returned an iterable `choices`.

Fix:
- Guard empty/null choices and raise a controlled empty-response error instead of crashing.

### Missing `message.annotations` crash
Symptom:
- Some provider responses did not include `message.annotations`.
- Accessing the attribute directly raised `AttributeError`.

Root cause:
- Client code assumed the field always existed.

Fix:
- Use `getattr` or equivalent guarded access.

## 2.2 Structured-output and list-output bugs

### False degeneration detection on structured output
Symptom:
- Pretty-formatted structured responses with whitespace triggered degeneration detection.
- Good structured outputs were treated as pathological repetitions.

Root cause:
- Degeneration heuristics were applied uniformly, even when `response_format` was present and the model was returning valid JSON-style structured content.

Fix:
- Structured-output paths needed separate handling or bypasses for whitespace-heavy formatting.

### Top-level list structured output required special handling
When list outputs were added, several bugs appeared:
- Wrapper/unwrapper ordering with URL restoration was wrong.
- Replay logic returned the wrapper instead of the actual list.
- Tool-loop final response paths forgot to thread list metadata.
- Content flags could leak between subclasses.
- Serialization logic assumed singular `BaseModel` outputs.

This was the best evidence that adding `PromptSpec[list[T]]` or `Document[list[T]]` support is not just a type-checking change. It impacts:
- request formatting,
- replay,
- content restoration,
- serialization,
- CLI labeling,
- strict-mode wrapper generation.

## 2.3 Observability and tracing bugs

### Laminar spans did not show failures correctly
Initial assumption:
- The problem was logger severity.

Actual root problem:
- The framework did not set OpenTelemetry error status on spans.
- Exceptions were not being recorded to the span.
- The Laminar backend also ignored status in some paths, so even correctly-set status could be dropped.

This produced an important lesson:
- “The UI does not show an error” may be a cross-system propagation bug, not a logging bug.

Framework-side fix:
- Set span status to error and record exception events on failed spans.

### Provider and flow failures were often visible only indirectly
Several application tasks swallowed provider failures into counters or partial results instead of surfacing them structurally. This meant the trace existed, but the failure was buried in data, not made explicit at orchestration boundaries.

## 2.4 Persistence, replay, and debug bugs

### Replay CLI content-loss bug
Symptom:
- Replayed runs lost document content and became unusable for debugging.
- The replay path wrote references without preserving enough filesystem-backed content.

Root cause:
- Replay execution lacked a proper writable sink database path or overlay pattern.

Fix:
- Introduce debug sinks / filesystem-backed replay artifacts so replayed runs have the same inspectability as original ones.

### `DebugSession` conversation handling created invalid documents
A framework debug helper tried to wrap conversation text into a bare `Document`. This was rejected by the framework because bare `Document` should never be instantiated. An internal subclass workaround then collided with a semgrep rule banning document subclasses inside framework internals.

Lesson:
- A debug API that needs a fake “document just to satisfy a return shape” is a design smell.
- This ultimately led to returning conversation output separately instead of forcing it into a document.

### Bare `Document.create_root(...)` needed explicit lint protection
The runtime already rejected bare `Document` creation, but static analysis did not catch `Document.create_root(...)`. A semgrep rule was added later, and it exposed another problem: the semgrep version in use could not parse PEP 695 syntax well enough to find violations in some files.

Lesson:
- Some safety guarantees require both runtime and lint-time protection.
- Tooling version mismatches can silently blind a rule.

## 2.5 Documentation and docs-generator bugs

### Public API visibility was often wrong for AI docs
Two recurring documentation failures appeared:
- Important subclass contracts were hidden because methods were prefixed `_` and therefore excluded from generated docs.
- Large non-user-facing APIs bloated the generated README and pushed it over size limits.

Examples:
- Provider subclass API was undocumented because key extension methods were private.
- The `logger` module generated a nearly useless guide because the only public symbol was an auto-configured setup function that application authors never needed.

Lesson:
- Documentation generation is only as good as the public/private boundary.
- If a method is part of a subclassing contract, hiding it as “private” creates broken guides.
- If a module has no meaningful user-facing surface, it should not be public.

### `.ai-docs` freshness failures after release changes
Version bumps and API changes repeatedly produced stale generated docs. This led to improvements in pre-commit automation and forced the realization that docs freshness must be treated as part of the change, not a follow-up step.

## 3. Recurrent Application Misuse Patterns

## 3.1 Task misuse

### Repeated LLM ceremony in every task
Many tasks repeated the same sequence:
- build spec,
- create conversation,
- send spec,
- parse result,
- derive output document.

This did not prove the framework was unusable, but it did prove that without guidance or a helper abstraction, application code will drift into repetitive, error-prone boilerplate.

### Tasks doing multiple logical jobs
Real tasks often combined:
- planning,
- review,
- repair,
- metadata extraction,
- control decision generation.

This created large tasks with multiple LLM calls, internal orchestration, and ambiguous outputs. The evidence strongly suggested that many “task complexity” problems were caused by developers not splitting work at natural decision boundaries.

### Hidden task orchestration
Tasks indirectly called other tasks through helper functions or chains hidden in module-level async functions. This bypassed the framework’s execution model and made traces lie about what really happened.

### Returning too much
Tasks often returned intermediate artifacts that were never consumed later, either because persistence and handoff were conflated or because the developer wanted to “be safe.” This was later identified as a central source of document bloat.

## 3.2 Flow misuse

### God-flows
Real flows exceeded 300-500 lines and contained:
- multiple internal phases,
- nested closures,
- custom parallel wrappers,
- document creation,
- provider I/O,
- control decisions,
- manual flattening,
- output filtering.

This is the single most visible failure pattern from application code.

### Flows creating documents directly
Flows built provenance, signals, routing, or summary documents inline rather than delegating this work to tasks. This bypassed the task execution model and made provenance harder to reason about.

### Flows returning everything
Flows frequently returned all evidence, all reports, all resolutions, all reviews, and all summaries “just in case.” This caused:
- huge downstream document bags,
- large unions in signatures,
- more filtering logic in later flows,
- unreadable cross-flow contracts.

### Monkey-patching framework metadata
Flows patched `input_document_types` or similar metadata at runtime to force multi-round behavior. This invalidated type-chain guarantees and became one of the strongest pieces of evidence for freezing framework metadata after definition time.

## 3.3 Document misuse

### Shuttle documents
Documents were created only to transport state from one place to another, often holding nothing but IDs, names, references, or wrapper lists. These were not meaningful business artifacts and inflated the document graph.

### Prompt-wrapper documents
Developers created fake documents solely to stuff text into LLM context because they lacked confidence in or awareness of existing PromptSpec field mechanisms.

### Model explosion and disconnected `models/` package
The application built a large `models/` package disconnected from document ownership. Content models lived far from their document classes, and wrapper models multiplied because list outputs and structured-result patterns were not designed clearly.

### Centralized “any” unions
Large unions like `AnySimplifiedDocument` were used to cope with heterogeneous document bags. They made signatures unreadable and encouraged fishing rather than typed contracts.

## 3.4 Provider misuse

### Custom provider gateways reimplemented framework features
Applications rebuilt:
- retry loops,
- polling,
- provider outcomes,
- testing utilities,
- result parsing,
- citation extraction.

This was a major duplication source and one of the clearest signals that provider guidance and primitives needed to exist and be discoverable.

### Summary misuse in provider documents
Search prompts or long query payloads were sometimes put into `summary` fields, causing validation failures because summaries have strict size limits. This became a concrete example of why document metadata must not be abused as arbitrary storage.

### Custom attachment reconstruction ignored framework serialization
Provider result reconstruction initially reimplemented bytes/data-URI handling instead of using the framework’s own document deserialization. This caused corrupted image attachments and unnecessary complexity.

## 4. Large-Scale Problem Families Identified from the Failed Application

Repeated audits converged on roughly 25 problem families. The exact grouping varied, but the stable families were:

### 4.1 Boilerplate and abstraction failures
- Repeated task LLM ceremony
- Repeated document extraction boilerplate
- Clumsy fan-out orchestration
- No clean primitive for transient coordination state
- Painful evolving-state document handling

### 4.2 Document and type-shape failures
- Multi-round accumulation expressed by monkey-patching
- Unbounded flow returns
- Massive input/output unions
- Paired artifacts drifting apart
- Wrapper-model proliferation

### 4.3 Structural and orchestration failures
- Nested closures in `run()`
- Tasks calling tasks
- Conversation use inside flows
- Monkey-patched type metadata
- God-flows and mega-tasks
- Hidden orchestration in helpers

### 4.4 Testing failures
- Heavy monkeypatching needed for task tests
- No clear provenance assertion helpers
- No clear mock tool-call helpers
- No prompt snapshot discipline

### 4.5 Documentation and discoverability failures
- No clear document litmus tests
- No guidance on task atomicity
- No guidance on flow return discipline
- No guidance on model placement or co-location
- No explicit “do not reimplement framework primitives” guidance

## 5. Proposal History: What Was Explored, Rejected, or Reframed

The analyses explored a large number of framework changes. Many looked attractive at first and were later rejected after closer scrutiny.

## 5.1 Proposals that looked necessary at first but were later dropped

### Auto-collect persistence of all created documents
Initially attractive because it seemed to decouple persistence from return values. Later rejected as a primary mechanism because the evidence showed very few legitimate cases of task-created documents that should be persisted but not returned, and because it risked persisting prompt-wrapper garbage if document discipline remained weak.

### PromptMaterial
Originally proposed to stop fake prompt-wrapper documents. Later dropped after repeated analysis showed that the existing PromptSpec field system already covers almost all legitimate cases if used correctly.

### DocumentGroup as a required framework primitive
Initially attractive as the solution to signature bloat. Later reframed as “nice to have, not essential” because composite documents and plain frozen models could cover many real use cases, and because a first-class group type required cross-cutting changes across too many modules.

### `map_tasks`
Initially proposed as the answer to closure-based fan-out. Later judged unnecessary because the framework already had `TaskHandle` plus `collect_tasks`, and the real problem was discoverability and calling style, not a missing primitive.

### `accumulated_input_types`
Originally proposed to solve multi-round flows. Later dropped when repeated analysis concluded that correct return discipline and composites solve most of the problem, and that hidden accumulation metadata makes signatures lie.

### Full `VersionedStateDocument`
Initially explored for mutable ledger-style state. Later simplified to the insight that immutable documents plus better ergonomics for versioning are enough in most cases.

### Declarative FlowSpec / graph DSL
Repeatedly proposed and repeatedly rejected. The evidence showed that real adaptive business logic contains sequential update loops, budget-gated iteration, and runtime decisions that become uglier when forced into a mini-language.

## 5.2 Proposals that remained strong after repeated challenge

### Named typed inputs
This survived every round. It was the only proposal consistently identified as the one change that materially transforms flow readability by eliminating most `find_latest` / `find_all` boilerplate.

### Deployment-side input resolution
This stayed coupled to named inputs. The important nuance that emerged later was that singleton resolution is easy, while collection scoping is harder and may still require an explicit filter line or an explicit resolution annotation.

### Task-in-task prohibition
This remained uncontested. It is a clear misuse pattern and easy to guard.

### Better provider abstractions
This survived because the application evidence showed repeated provider duplication, and later real provider implementation work validated the need.

### Model/document co-location
This survived strongly. The evidence from the `models/` package explosion showed that free-floating content models are a root cause of unreadability.

## 6. The Strongest Lessons Learned

## 6.1 The framework was often blamed for problems caused by misuse
Repeated analysis eventually showed that many proposed framework additions were reactions to bad application code, not genuine framework deficiencies. Existing features already covered many cases:
- PromptSpec fields could replace prompt-wrapper documents.
- TaskHandle plus `collect_tasks` already handled fan-out.
- Composite documents could reduce many large unions today.
- Deployment accumulation already preserved documents across flows.

## 6.2 The remaining real framework questions are API-shape questions
The most durable findings were not “add more machinery,” but:
- make the correct API shape easier,
- stop exposing bag-of-documents calling styles,
- make subclass contracts visible in docs,
- freeze metadata that should never be mutable,
- express control flow through explicit control documents.

## 6.3 Detection alone is not enough
Late-stage analysis concluded that some attractive enforcement ideas were too detection-heavy:
- line limits,
- naming warnings,
- anti-pattern warnings,
- AST bans used as primary safety mechanisms.

The evidence consistently pushed toward a stronger standard:
- if the framework can remove the need for a bad pattern, that is better than warning about it afterward.

## 6.4 Some “simple fixes” created new problems
Examples:
- Turning bare conversation output into a fake document created a framework violation.
- Using runtime persistence hooks to save everything would have persisted invalid scaffolding.
- Making provider subclass APIs private produced unusable generated docs.
- Adding list output support without replay/serialization work introduced subtle bugs across multiple layers.

## 7. Historical Bottom Line

By the end of the analysis history, the evidence supported three conclusions:

1. **The failed application was not a proof that the framework must be rebuilt from scratch.**
   It was proof that wrong patterns can produce catastrophic complexity when the framework does not strongly shape how code is written.

2. **Some framework changes are justified, but far fewer than first assumed.**
   The evidence consistently narrowed the “must-have” list as more proposals were stress-tested against real code.

3. **The most valuable historical artifact is not any one proposal.**
   It is the corpus of concrete failures:
   - type/runtime mismatches,
   - god-flows,
   - tuple-bag APIs,
   - model/document separation,
   - provider duplication,
   - fake documents,
   - return-everything behavior,
   - hidden orchestration.

Those failures are the benchmark future framework changes must be judged against. If a proposed fix does not clearly prevent one of these observed classes of failure, it is probably not the right fix.