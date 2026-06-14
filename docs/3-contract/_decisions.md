# Contract Decisions

This ledger records the contract-shaping decisions behind the 0.3.0 public contract. It is not required reading for
authoring or operating an application — the contract files and `api/` are the contract. It exists so the reasoning
behind the contract's shape is recoverable without re-reading the conversations that produced it.

## The contract is written as version 0.3.0, and the implementation is built to satisfy it

**Decision.** The public contract is written in present tense as version 0.3.0, the version that is, and the
implementation is built to satisfy it. Conformance is verified by the implementation and testing layers, not
asserted here; this ledger records the contract's shape, not a conformance claim.

**Why.** Fixing what an application may rely on keeps implementation accidents from hardening into promises, and no
contract file has to hedge about whether a surface is built yet. Contract files carry no migration notes, no
deprecation markers, and no references to any prior public shape.

## Continuation is class-declared, with no public `continue_from=`

**Decision.** Multi-step conversation continuation is declared on the contract class with
`continues=Continues.once(Opener)` or `continues=Continues.repeating(Opener)`. The legal predecessor set is
`{Opener}` for `.once` and `{Opener, Self}` for `.repeating`. The predecessor enters through a `PromptResult`-typed
field. There is no `continue_from=` parameter on `.execute(...)`.

**Why.** Declared lineage is checkable at import time, so independence between a producing judgment and a contract
that reviews it is provable from the declaration rather than discovered at runtime. A runtime continuation kwarg
would make anchoring a property of execution wiring, defeating the import-time guarantee. See
`api/4-prompt-contracts.md § Multi-step continuation is declared, not improvised`.

## `AIModelRef` is the model handle

**Decision.** The public model identity is `AIModelRef` everywhere — constructed at the configuration boundary,
supplied through `RunConfig`, passed to `.execute(model=...)`, and never inspected by application code.

**Why.** A single opaque handle keeps provider selection, routing, and caching behind the framework boundary and
prevents application code from coupling to model identity. One name across the whole surface removes the
task↔contract type mismatch a split vocabulary would create.

## No public `Conversation`

**Decision.** There is no public `Conversation` surface. A model-mediated judgment is a `PromptContract`;
multi-turn work is declared continuation between contracts.

**Why.** A general conversation object invites chat-shaped, improvised multi-turn code that escapes the import-time
guarantees the contract surface provides. Every model interaction is a declared, typed judgment.

## No backward compatibility in the public shape

**Decision.** The public authoring surface is the one correct shape; the contract does not preserve alternative or
legacy shapes for compatibility.

**Why.** Multiple valid shapes for one intent are the divergence machine-speed authorship copies. One shape per
intent is the mechanism that keeps authored code convergent, and the framework rejecting the others is the product
working (`4-limits-and-non-promises.md`).

## Caching is task-level and content-addressed

**Decision.** Caching is owned by the framework at the task boundary, keyed by content (task inputs and
configuration). A run selects a cache policy (`5-configuration.md`); it does not tune cache mechanics.

**Why.** Content-addressed task caching makes a rerun after a fix reuse already-correct work and recompute only
what changed — the resume-after-failure value, not generic speed. Coarser or author-tuned caching reuses results
from work that should have been invalidated.

## `PromptResult` carries response and citations only

**Decision.** `PromptResult` exposes the typed response and its citations, and nothing else. Cost, duration, span
identifiers, model identity, and transport metadata are recorded in the run record, not on the result object.

**Why.** Keeping operational metadata off the authoring result object stops applications from coupling to it and
keeps the authoring surface small. Cost and execution facts are read from the record through the read seam
(`advanced-api/2-database.md`).

## `validate()` reads only self and response

**Decision.** Contract-level validation is synchronous and reads only `self` and the parsed response. It performs
no I/O, randomness, clock reads, service calls, global-state inspection, or mutation.

**Why.** Validation that can reach outside the response is non-reproducible and turns a shape check into hidden
business logic. A pure validator keeps the repair loop deterministic and the accepted-response definition legible.

## Citations are structural and document-backed

**Decision.** Evidence-grounded claims are modeled with `CitedText`, whose citations point at durable pipeline
documents. `DocumentCitation` has one canonical, identity-based shape —
`{document_id, url, title, start_index, end_index, field}` — defined once in `api/3-content-models.md § CitedText`.
A `CitedText` citation is document-backed (`document_id` set); provider URLs and external-source strings are not
`CitedText` citations, and external evidence is captured as a document before generated prose can cite it. The
framework may additionally surface engine URL citations (`url` set, `document_id`/`field` `None`) at the
`PromptResult` level; those are result-level metadata, not `CitedText` support.

**Why.** Document-backed citations keep the link between a claim and its evidence inside the provenance graph,
where it can be traced and challenged. One identity-based shape stated in one place removes the
`{document, quoted_text}` versus `{document_id, ...}` contradiction an authoring agent would otherwise compile
against; separating engine URL citations as result-level metadata keeps the `CitedText` promise — support is a
document — intact while still surfacing what search-enabled models return.

## Provenance records immediate upstream only

**Decision.** A document factory records only its immediate upstream documents — immediate content sources, causal
sources, external source, or prior revision. Application code does not copy ancestors forward to make the chain
visible locally.

**Why.** The framework recovers full ancestry through links; copying ancestors creates false lineage and bloats
the record. Immediate-upstream-only keeps provenance faithful and the document model clean
(`api/2-documents.md`).

## One `ai-pipeline` CLI with subcommands

**Decision.** The framework ships exactly one operator command, `ai-pipeline`, with a closed set of subcommands:
`init`, `verify`, `plan`, `run`, `test`, `inspect`, `replay`, `cancel`, `recover`, `sync`, and `deploy`. There are
no separate `ai-verify`, `ai-run`, etc. binaries. `init` scaffolds a fresh project; `test` runs the application's
tests (single units exercised under the runner with full recording) and is the correctness gate; `inspect` absorbs
the markdown trace-bundle workflow that was a separate trace-inspector tool. Single-unit execution is a mode of
`run` (and the substrate `test` stands on), not a separate command. The per-subcommand contracts live one file per
subcommand in the `tools/` subtree. `dev` is a separate external contributor tool — it lints, tests, and releases
ai-pipeline-core's own source — and is not part of ai-pipeline-core.

**Why.** A sprawl of independent `ai-*` binaries is hard to discover and misleading about what is one tool; the
operating surface is copied and learned the way the authoring surface is, so a fragmented operating surface
fragments operator behavior the way unstructured authoring fragments code (`../2-problem/3-hard-parts.md`). One command
with uniform subcommands keeps the operating surface small, discoverable, and consistent — the operating analog of
the one-correct-shape-per-intent rule — and is the contract face of the *Not making each application stand up and
operate its own machinery* non-goal. This supersedes the earlier decision that every operator command carries the
`ai-` prefix. `init` and `test` are net-new; the rest are the prior capabilities re-homed as subcommands. There is
deliberately no `serve` subcommand (see _Local execution has two profiles; there is no `serve`_ below).

## The single CLI emits structured, agent-consumable output

**Decision.** Every `ai-pipeline` subcommand offers a structured, machine-parseable output mode alongside its
human-readable output, and every error names its concrete fix. Structured output is a documented convention of the
surface, not a per-subcommand afterthought.

**Why.** The operator is an AI agent, and the benchmark/diagnosis loop is automated: an agent reads `run`, `test`,
`inspect`, and `replay` output programmatically to compare runs and decide where a cheaper model suffices. Without a
stable structured form, "the development loop is operable by agents" (`../2-problem/2-constraints.md`) is not actually
operable from the contract. The precise flag spelling is illustrative mechanism; that a machine-parseable form
exists, and that errors carry their fix, is contract.

## Local execution has two profiles; there is no `serve`

**Decision.** Local use comes in two profiles. The **in-process profile** runs `verify`/`plan`/`run`/`test`/
`replay`/`inspect` with zero external infrastructure, filesystem-backed, producing the same record a full run
produces. Full runtime/API parity locally is the **local-full-stack profile**: stand up the supported runtime where
the work is developed and `deploy` to it, then operate it through the same runtime surface used in production. There
is no `serve` subcommand that runs a partial local API.

**Why.** This is how *Operating a run must not depend on where it runs* (`../2-problem/2-constraints.md`) is delivered
honestly: the in-process tools are genuinely zero-infra; the HTTP runtime always needs the supported stack, local
or remote, and is reached the same way in both. A `serve` mode would be a third, partial way to operate that
diverges from production — exactly the divergent-local-mode non-goal. The compose topology that stands the stack up
locally is Layer-4/5 mechanism.

## Endpoint shapes and worked examples are contract; the transport binding is the supported binding

**Decision.** The `runtime-api/` surface is written as a concrete endpoint reference: the request, response, event,
and error **shapes** are contract and are shown concretely with worked examples, the way `api/` shows class shapes,
because an external consumer cannot import them. The specific HTTP method and path are shown concretely as the
**binding the supported runtime ships**, annotated as such rather than frozen as the immutable contract; the
transport's status-code mapping, the specific event-bus and orchestrator products, the server framework, and the
trust/exposure layer remain Layer-4/5 mechanism.

**Why.** The maintainer ruling is that `3-contract/` is the only layer the operating agent reads, so the runtime
surface must be usable as an integration reference, not semantic prose. This supersedes the earlier framing that the
runtime surface stays purely semantic because "the wire is mechanism": the shapes a consumer constructs and parses
are break-sensitive and therefore contract, while the transport that carries them still varies underneath. How
literally the HTTP method+path are frozen is recorded as an open decision below.

## `CachePolicy` is a closed set

**Decision.** A run's cache behavior is selected with `CachePolicy`, a closed set of `REUSE`, `REFRESH`, and
`BYPASS`, defined in `advanced-api/1-runners-and-clients.md`. It selects policy, not cache mechanics.

**Why.** `REFRESH` (recompute and keep the cache populated) and `BYPASS` (recompute and leave the cache untouched)
are genuinely different operator intents, and a closed enum keeps the choice explicit rather than encoded in a
boolean. Caching itself stays framework-owned and content-addressed.

## Read-seam record names and the first-class run read model

**Decision.** The read seam exposes a first-class run read model — `get_run` (one `RunSummary` by `run_id`,
including observed `state` and error) and `get_run_outputs` (the delivered bundle of a completed run) — above the
span-walk and provenance methods. Its record types are `RunSummary`, `SpanRecord`, `DocumentEvent`, `LogRecord`, and
`CostTotals`, and a single contract run returns a `ContractRunResult` carrying `run_id`, status, and the
`PromptResult`. The seam also exposes `get_span(span_id)` — one span by its own identity, without the parent
`run_id` — which a single-unit replay (`tools/7-replay.md`) stands on. `RunSummary` carries `state: RunState` (not
`RunStatus`) and `execution_id`; `CostTotals` exposes `cost_usd` alongside its token totals.

**Why.** The runner/client seam exists so adjacent systems read authoritative run state directly instead of
reconstructing it from low-level span walks. A run summary, a delivered result, and a terminal status by `run_id`
are the first-class reads; spans and provenance are for descending afterward. Naming the record types and the
single-contract result type fixes the public surface those consumers project. The read seam itself now lives in its
own file, `advanced-api/2-database.md`, alongside the write seam (see _The database API splits reader and writer
into its own file_ below); `advanced-api/1-runners-and-clients.md` retains the runner and points to it.

## Configuration names categories, not keys

**Decision.** `5-configuration.md` commits as contract to the two configuration channels (`RunConfig` and
environment configuration) and to the *categories* of environment configuration (model gateway, persistence
backend, observability sink, event sink, orchestrator/runtime backend, operational limits). The concrete
environment-variable names and backend families it shows are illustrative examples, not frozen contract.

**Why.** Configuration is public, but freezing specific key spellings and backend identities as contract would
couple applications to transport and storage choices that should stay behavior-facing. Committing to the categories
keeps configuration usable without making a key rename a contract break.

## Tasks are correct wherever they run

**Decision.** The contract requires every task to be correct wherever it runs. Authored application code does not
depend on process placement, same-process continuity across task boundaries, or mutable module-level globals that
hold run-affecting state. The contract names no execution mechanism.

**Why.** This keeps placement-independence a property of the authored shape rather than a feature. When every task
depends only on declared document inputs, declared fields, and shared durable state, later execution changes do not
require a contract break. The framework is free to move task execution without changing authored applications.

The one allowed mutable process-local value is transport infrastructure behind an integration boundary: an async
HTTP session, SDK client, connection pool, or similar client-side plumbing. That state is recreated per process, is
never serialized or persisted, carries no run state, and does not affect what a task means or what outputs it
produces. The exception exists to keep the contract practical without weakening its guarantee.

## Side-effect idempotency is recorded as a note, not contract text

**Note.** A side effect that repeated execution would duplicate can be made safe to repeat through a durable
identity carried in pipeline state, typically its receipt document, so a task's correctness need not depend on
having run exactly once in one process. This is kept here as rationale only; it is deliberately not stated as a
requirement in `api/6-tasks.md`.

**Why a note.** Placement-independence alone does not require a duplicate-delivery guarantee, so the contract makes
no such promise and the task boundary carries no idempotency rule. Recording the consideration here preserves the
reasoning without committing the contract to it, and names no execution mechanism. Any stronger delivery semantics
belong to the side-effect and receipt surfaces, not to task placement rules.

## The runtime, operation, and integration plane is in scope

**Decision.** Operating, observing, halting, recovering, and reading back a single run — and standing up the
runtime that executes it — are in scope for the contract. Accumulating, scoring, or reasoning over state across
many runs, rendering a user interface, warehousing the record for analytics, and the authentication, authorization,
and trust boundary stay out.

**Why.** The API, middleware, runtime, and one-command deploy now live in this repository, with no backward
compatibility to a prior shape. The contract must own the semantic surface for operating a run, or every consumer
rebuilds it with bespoke glue or by reaching into internals. The line is drawn at the run, mirroring the problem
frame's non-goals; `1-what-ai-pipeline-core-is.md` and `4-limits-and-non-promises.md` are brought into lockstep
with it.

## Two outward surfaces: in-process and out-of-process

**Decision.** The contract distinguishes the in-process programmatic surface (`advanced-api/`), used by code that
drives a run inside its own process, from the out-of-process runtime surface (`runtime-api/`), used by an external
system that operates a run over a boundary it did not author. The out-of-process surface is its own contract area,
separate from `api/`.

**Why.** The two are distinct contracts with distinct readers; collapsing them — making the author learn the
operating surface, or the integrator reach through the authoring surface — bloats one and starves the other.

**Directory name (committed).** The out-of-process surface is `runtime-api/` — a sibling of `api/` and
`advanced-api/`, not additional files under `advanced-api/`. The name `integration/` was rejected because
`api/9-integrations.md` already means application-authored *outbound* service clients; `runtime-api/` names the
framework runtime plane an external system operates against, and matches the existing sibling pattern (`api/` for
authoring, `advanced-api/` for in-process driving, `runtime-api/` for out-of-process operation). The earlier
hedging phrase "the runtime and integration surface" in the root files is replaced by direct references to
`runtime-api/`.

## Live delivery is best-effort; the durable record is authoritative

**Decision.** The live observation signal is best-effort and at-least-once; every lifecycle transition is recorded
independent of whether live delivery succeeded, and the durable record is the authoritative account. A consumer
merges a live stream with a record-derived catch-up read and deduplicates against the record.

**Why.** Observation must survive disconnection and missed delivery, and a watcher connects late, drops, and
reconnects. A record-authoritative model lets a watcher recover exactly the stretch it missed and never makes the
last observed signal the truth. Exactly-once delivery is named as a non-promise in `4-limits-and-non-promises.md`.

## The externally readable record is versioned and self-describing

**Decision.** The record, results, and documents an external system reads are versioned and decodable without the
producer's code, and a reader that meets a shape it does not understand fails loudly with a version mismatch rather
than silently mis-decoding.

**Why.** The record is read by systems built separately, possibly on a different version, that cannot load the
producing application's definitions. This is the constraint the internal serialization work must satisfy; stated as
a contract promise, it keeps any later change to how values are serialized from silently breaking external readers.
How serialization is performed is a design concern, not contract.

## One run identity addresses a run across every surface and over time

**Decision.** A run has one stable identity that addresses it coherently wherever it is launched, observed, halted,
recovered, or read, and that stays disambiguable when the same identity is reused across attempts over time.

**Why.** A run is launched in one place, observed in another, and read in a third; without one identity, every
integrator builds its own correlation scheme and every recovery guesses which attempt it is recovering. This
extends the `run_id` addressing already used under _Read-seam record names and the first-class run read model_ to
the operating surfaces and to reuse over time, and is distinct from document identity.

## Logs are a first-class durable read surface

**Decision.** Execution logs attach to their unit, are durably readable by run and by unit with level and category
filters, and are part of the supported record. The in-process read seam exposes them through `get_run_logs` and
`get_span_logs` (`advanced-api/2-database.md`); the out-of-process read model projects the same reads
(`runtime-api/5-documents-and-logs.md`).

**Why.** Recorded execution is the only debugging and verification substrate, and the development loop is operated
by agents and external operators who read logs to diagnose and recover a run, so logs are a first-class read
surface rather than incidental output. Naming the read-seam methods closes the gap between the guarantee, which
promised readable logs, and the seam, which exposed no log read. The honest edge — near-zero-tail-loss, not
zero-loss — is named in `4-limits-and-non-promises.md`.

## Bounded retry budget is configurable; retry policy is not

**Decision.** A bounded retry budget is a process-wide setting under environment configuration; retry policy and
mechanics — when and how the framework retries, paces, and times out — remain framework-owned and not tunable per
call.

**Why.** A bounded retry budget is operational scale, not retry behavior; separating the budget (a process
setting) from retry policy and mechanics (framework-owned) keeps the budget from reading as permission to tune
retry per call. The split is precedented by the prompt-contract repair budget.

## Configuration adds an event sink and an orchestrator backend

**Decision.** Environment configuration commits to two further categories — an event sink (the live lifecycle bus)
and an orchestrator/runtime backend — as categories with illustrative keys, not frozen names.

**Why.** The runtime the framework now owns needs an event bus and an execution backend; this is consistent with
the decision that configuration names categories, not keys.

## A supported runtime topology is promised; its mechanics are not contract

**Decision.** The framework promises a maintained, versioned runtime topology — the cooperating services a run
needs — stood up as one verified whole. The compose files, scripts, ports, and topology mechanics are design and
implementation, not contract, and host networking, external exposure, autoscaling, and the trust boundary are not
promised.

**Why.** Standing up the operational stack is the same generic burden for every adopter; promising the topology
keeps that burden off each adopter, while keeping the mechanics below the contract keeps the contract from becoming
infrastructure documentation.

## The observed run-state is one shared broader type

**Decision.** A run's observed state is one shared, broader type — `RunState` = `{PENDING, RUNNING, COMPLETED,
FAILED, CANCELLED, CRASHED}` — defined once in `runtime-api/1-overview-and-state.md` and used everywhere a run's
state is read, reconciled, observed live, or notified. It is not a separate external state model. The in-process
synchronous result (`advanced-api/1-runners-and-clients.md`) reports only what a blocking call can observe —
`COMPLETED`, `FAILED`, `INCOMPLETE` — which overlaps `RunState` on `COMPLETED` and `FAILED`; `INCOMPLETE` is the
blocking-call name for a run that returned before reaching a terminal `RunState` and is not a `RunState` member.

**Why.** A run is launched in one place, observed in another, recovered in a third; a single broader state
vocabulary lets every surface speak the same language and keeps the in-process result an overlapping reading of it
rather than a rival enum. The watcher-only members `PENDING`, `RUNNING`, `CANCELLED`, and `CRASHED` are exactly the
distinctions the guarantees (`§ Live observation`, `§ Governable in-flight runs`, `§ Reconcile to recorded truth`)
already require.

**There are exactly two run-level types, not three; a third was rejected.** `RunState` is the observed run-state
read everywhere a run's state is read (run summaries, list reads, reconcile reads, live events), and `RunStatus`
(`{COMPLETED, FAILED, INCOMPLETE}`) is only the blocking in-process call result. A raw record read (a `RunSummary`
through `advanced-api/2-database.md`, a run summary or list read through `runtime-api/4-run-record-read-model.md`)
returns the *recorded* `RunState` — `PENDING | RUNNING | COMPLETED | FAILED | CANCELLED` — and the reconcile read
(`runtime-api/2-run-control.md`) is the only producer of `CRASHED`. That raw-versus-reconciled difference is
**endpoint authoritativeness**, not a separate type: a proposed third type (`RecordedStatus`) was considered and
**rejected**, because it would force an integrator to juggle two near-identical six-value enums for the same run
depending on which endpoint it called, and `RecordedStatus.running` carries the identical un-reconciled ambiguity as
`RunState.RUNNING`. The per-unit statuses `cached` (a unit served from cache) and `skipped` (a gated or bypassed
unit) are **span-level**, carried on `SpanRecord.status` / `run_unit.status`; they are not run-level `RunState`
values, and their earlier appearance on the run summary field and run-level list filter was a leak, now corrected.
`RunSummary.status` (in-process) is renamed to `state: RunState` and gains `execution_id` to match the out-of-process
summary.

## The database API splits reader and writer into its own file

**Decision.** The public database API — `DatabaseReader` (the read seam) and `DatabaseWriter` (the write seam for
the framework runtime and specialized in-process producers) — lives in its own file, `advanced-api/2-database.md`.
`advanced-api/1-runners-and-clients.md` retains the runner and points to it. The writer is append- and
record-oriented (append spans, save documents and content, save logs, update summaries, flush/shutdown) and is
never reachable from application authoring code; the reader is read-only and authoritative.

**Why.** The runner file had grown to carry both running a pipeline and the entire read seam; splitting the database
API out keeps each file to one surface and gives the write seam — needed by the runtime that records runs and by
framework-adjacent producers — a documented home without loosening the authoring rule that application code never
touches the store (`api/9-integrations.md`).

## Operation is uniform across placements, and launch is non-blocking

**Decision.** The contract guarantees that the same application is operated identically — same commands, same
runtime surface, same record, same inspect/replay — whether it runs in one local process or across many for
production; that launching a run is non-blocking and returns the run identity without waiting for completion, and
that a deployed pipeline accepts runs promptly without a per-run rebuild (deploy once, launch many); and that a run
is observable by subscription or by polling the record, interchangeably, both reconciling to the authoritative
record (`3-guarantees.md`).

**Why.** These discharge the Layer-2 forces *Operating a run must not depend on where it runs* and *The
change-to-result loop must stay short*. They are stated as structural promises (parity, non-blocking, prepared
operation, observe-either-way), never as a wall-clock latency number — the no-SLA boundary is recorded in
`4-limits-and-non-promises.md`. *Location independence* (already a guarantee) covers correctness across placements;
operational parity is its operator-facing twin, covering the *operation*.

## The recorded corpus is a benchmark substrate; scoring stays the consumer's

**Decision.** The guarantee that every run is preserved, traceable, and replayable is stated to also make the
accumulated record of past runs — including production runs — a reusable benchmark and regression substrate: every
run durably records its inputs and outputs and is replayable against them, with cost attributed down to the
judgment. The framework provides the substrate; the methodology that compares runs and the judgment of which result
is better belong to the consuming agent (`3-guarantees.md`, `4-limits-and-non-promises.md`).

**Why.** This discharges the Layer-2 force that whether a cheaper way of making a judgment suffices is empirical and
can only be measured against recorded behavior, while holding the bright line: the framework records and re-executes;
it is not a benchmarking or analytics engine (`../2-problem/4-non-goals.md § Not an analytics or reporting warehouse`).

## The durable-log guarantee is one cross-subsystem account

**Decision.** The *Durable, readable logs* guarantee is the contract face of the Layer-2 force *A run must be
understandable from one diagnostic account*: the diagnostic output of every part that touched the run — the machine,
the executing and scheduling machinery, the operating surface, the application's steps, and the external services it
called — is gathered into one run-correlated account, durably readable by run and by unit, filterable by level and
category. The log reads (`advanced-api/2-database.md`, `runtime-api/5-documents-and-logs.md`) return that unified
account, not only the application's own span logs.

**Why.** A diagnosing agent with bounded working context cannot assemble a per-subsystem account reached separately;
the contract must promise the account is reachable together, tied to the run. How the accounts are unified and
stored (the single sink, transport, schema, retention) is Layer-4 mechanism. This extends the earlier decision
_Logs are a first-class durable read surface_ above, which fixed that each line attaches to the unit that produced
it and is read through the seam: the two are compatible — lines still attach to units, and the run-level account
aggregates those lines across every cooperating part.

## The framework provides one supported way per concern

**Decision.** The contract is not broadly configurable or pluggable: for each generic operational concern
(persistence, the orchestrator/runtime backend, the event/notification transport, the deploy target, the run-log
store) the framework provides one supported way, selected by configuration but not a menu of interchangeable
mechanisms. The single exception is the model gateway, where model selection may target different providers through
`AIModelRef`. The backends behind these concerns never surface in the authoring surface (`api/`): an author neither
names nor configures them (`4-limits-and-non-promises.md`, `5-configuration.md`).

**Why.** This is the contract face of the *Not a broadly configurable or pluggable framework* non-goal. Every
interchangeable option is another visible shape the next agent copies and another axis of divergence; supporting
many alternatives multiplies the machinery the framework must keep correct; one chosen way can be made turnkey and
fast to stand up where a menu cannot. The model-provider exception is force-justified: several providers occupy one
role the author and operator already meet as a single shape, so admitting them adds no new shape to copy.

## Child-pipeline placement stays declarative only

**Decision.** The child-pipeline contract is the single declarative, placement-independent `f.child_pipeline`
boundary; local and remote behave identically and placement is framework-owned (`runtime-api/6-child-pipelines.md`,
`api/9-integrations.md`). The author always passes the concrete child `Pipeline` class and never a remote-target
reference; the framework resolves that class to a local in-process execution or a deployed remote run. The
imperative remote-child-from-task-code pattern that real consumer applications use today — invoking a remote child
imperatively with a fallback, reaching a private poll-timeout global — is **not** blessed as an alternative.

**Why.** The imperative shape is exactly the internal-reaching the 0.3.0 contract replaces, and a second authored
way to invoke a child is the shape-divergence this frame exists to prevent; those applications are migration targets
that will adopt the declarative boundary. The one real gap it exposes — a long-running child's poll-timeout bound —
belongs as a declared `RunConfig`/child bound if a need is later observed, not as a reached-into internal. This
supersedes the earlier open framing; `runtime-api/6-child-pipelines.md` records the legacy imperative pattern as a
migration target, not contract.

## The runtime interop shapes are contract; the HTTP method+path are the supported binding

**Decision.** The request/response/event/error **shapes** in `runtime-api/` are the break-sensitive contract — an
external consumer constructs and parses them and cannot import them, so they are shown concretely with worked
examples the way `api/` shows class shapes. The HTTP method and path shown alongside each capability are the
**binding the supported runtime ships**, concrete and usable as written but not frozen as contract independent of
that runtime: they are annotated as the supported binding (the way `5-configuration.md` annotates env-var names),
and may change as the shapes stay stable. The transport's status-code mapping, the event-bus and orchestrator
products, the server framework, and the trust/exposure layer remain Layer-4/5 mechanism.

**Why.** The maintainer ruling is that `3-contract/` is the only layer the operating agent reads, so the runtime
surface must be usable as an integration reference. The shapes a consumer breaks on if they change are therefore
contract; the transport that carries them still varies underneath, so freezing one method+path spelling forever
would couple the contract permanently to one transport and reverse the Layer-4 boundary on transport mechanics. This
supersedes the earlier open framing; `runtime-api/1-overview-and-state.md` states the same posture inline.

## Testing is a workflow over the existing surfaces, in its own `testing/` subtree

**Decision.** Test-authoring guidance lives in `testing/`, a sibling subtree of `authoring/`, holding the testing
guides and references (`testing/1-overview.md`, `testing/2-writing-tests.md`,
`testing/3-regression-and-benchmarking.md`). Testing is **not** a public authoring role: there is no
`api/10-testing.md` and no `*Test` role; `api/` stays the five authoring roles in files 1–9. A test is ordinary
Python that exercises a unit through the runner (`advanced-api/1-runners-and-clients.md`) with full recording; the
`ai-pipeline test` command (`tools/5-test.md`) runs the tests, and `tests/` is package layout, not a role.

**Why.** A first-class `ai-pipeline test` command without an authoring home led capable readers to invent a new
authoring role (`api/10-testing.md`). Testing is a workflow over the runner, read seam, and operator surfaces, not a
declaration the framework validates, so it belongs in a guidance subtree parallel to `authoring/`, not in `api/`.
The maintainer ruled the subtree placement.

## `DEGRADE` records a framework-owned `ItemFailure` document

**Decision.** Under a degrading fan-out (`api/7-phases.md`, `ItemFailurePolicy.DEGRADE`), the framework records the
failure document for each terminally-failed item, because the failed task raised rather than returned. The document
content is a framework-owned `FrozenBaseModel`, `ItemFailure`, carrying `error_message` and `failed_item_id`; the
author declares only the document *type* (`Document[ItemFailure]`) so later phases can select degraded items by
type, and the framework populates the content from the surviving `TerminalError` and records the failed input item
as a causal source.

**Why.** Documents require typed content created through provenance-recording factories, and the framework cannot
populate an author-defined content model whose fields it does not know. A framework-owned content shape keeps
content ownership honest — the framework builds what it can know — and rejects the alternative of duck-typed
author-declared fields the framework would inject by reflection.

## Out-of-process root inputs carry encoded raw material, decoded by `class_name`

**Decision.** A root input's inline `content` over the runtime boundary (`runtime-api/2-run-control.md`) is raw
external material in the shape its target `class_name` decodes: a plain string for a `Document[str]`, a JSON object
(validated against `document_input_schema`) for a `Document[FrozenBaseModel]`, and a base64 string for a
`Document[bytes]`. The consumer never constructs a typed `Document[T]` over this boundary and signals no encoding
separately; the runtime decodes against the target class.

**Why.** `content: str | None` could not carry a structured or binary root document, contradicting the descriptor's
advertised `document_input_schema`. Keying the encoding to `class_name` honors the schema, covers all three
`Document` content shapes, and preserves the intentional asymmetry: the in-process driver constructs typed roots,
the out-of-process driver supplies raw material the runtime resolves (`api/2-documents.md`).

## `estimated_minutes` is authored phase metadata

**Decision.** A phase may declare `estimated_minutes: ClassVar[int]` (`api/7-phases.md`), an optional authored
estimate the runtime surfaces in the integration descriptor and reconstruction (`runtime-api/2-run-control.md`,
`runtime-api/4-run-record-read-model.md`); when a phase omits it the descriptor reports `0`. It is planning
metadata, not a bound the framework enforces.

**Why.** The descriptor exposes `estimated_minutes` for a freshly deployed pipeline that has no run history yet, so
the value must be authored rather than derived from the recorded corpus; a `ClassVar` matches how `purpose` and
`returns` are authored on prompt contracts.

## Record field names: one 0.3.0 name, one annotated wire spelling

**Decision.** Each record field has one canonical 0.3.0 name used in prose and in-process APIs, with the differing
wire spelling annotated alongside on the out-of-process read models. Dollar cost is `*_usd` everywhere
(`total_cost_usd`, `cost_usd`, including in-process `CostTotals.cost_usd`); the execution attempt is `execution_id`
(wire `deployment_id`) and the run-root is `root_execution_id` (wire `root_deployment_id`); document-identity arrays
are `input_document_ids`/`output_document_ids` (wire `*_sha256s`); a model-mediated unit is a `judgment` (wire
`prompt_execution`). The wire token `deployment` denotes a pipeline *run/execution*, distinct from a published
*deployment* (`deployment_name`).

**Why.** `advanced-api/2-database.md` and `runtime-api/4`–`5` each restate the record shapes, and they drifted in
name and type. One canonical name plus an annotated wire spelling keeps the surfaces consistent without renaming the
wire fields, which would be an implementation change.

## Detailed runtime-surface decisions are settled in `runtime-api/`

**Note.** The out-of-process surface is now written as `runtime-api/`, and the decisions previously deferred to it
are settled there against the root-level decisions above:

- **Observed run-state membership.** Settled as the one shared `RunState` above
  (`runtime-api/1-overview-and-state.md`).
- **Driver-side root-document construction.** An out-of-process driver supplies each root input as raw external
  material — inline content or a fetchable reference — that the runtime resolves into a typed root document at the run
  boundary (`runtime-api/2-run-control.md § Root inputs enter as raw external material`); an in-process driver builds
  the root bundle through `advanced-api/1-runners-and-clients.md`. By-identity rehydration of a stored document is an
  in-process capability only; the out-of-process boundary has no by-identity input.
  `api/2-documents.md § Root documents are created at the run boundary` now points to both.
- **Lifecycle-event payload and cursor semantics.** Settled in `runtime-api/3-observation-and-notifications.md`:
  the event carries run identity, unit identity, the enclosing run-root identity, parent unit, a monotonic sequence
  number, status, documents, error, and the delivered result; catch-up is reconstructed from the record, addressed
  by an opaque monotonic cursor, at-least-once with an overlap window, deduplicated by (unit identity, transition
  kind).
- **Serialized wire field list.** Settled as the named, semantic fields of each read model
  (`runtime-api/4-run-record-read-model.md`, `runtime-api/5-documents-and-logs.md`); the wire encoding stays
  Layer 4.
- **Remote or cross-deployment child invocation.** Settled as one placement-independent child-pipeline contract —
  local and remote behaving identically, placement framework-owned (`runtime-api/6-child-pipelines.md`);
  `api/7-phases.md` and `api/9-integrations.md` point to it.
- **The advanced database write seam.** Settled in `advanced-api/2-database.md` (see above).
- **Externally-readable document visibility.** Settled: there is **no** `publicly_visible` marker (it was unused),
  and no per-document-type gating replaces it. Authorization is at the run boundary: a consumer authorized to read a
  run can read every document that run recorded, by identity or provenance walk; the delivered output bundle is the
  curated top-level result (`runtime-api/5-documents-and-logs.md § What is surfaced beyond the delivered outputs`).
  Visibility was never on the authoring surface (`api/2`) and is not added there.
- **Document `description` and `summary`.** Settled: these are framework-owned read-model metadata that the
  framework may attach (a producer-supplied label at the run boundary, or a framework-generated summary recorded
  through the write seam — `advanced-api/2-database.md`), surfaced on the document read model
  (`runtime-api/5-documents-and-logs.md`). They are not fields of the authoring document wrapper, which carries
  `name` only and keeps descriptive prose on the content model (`api/2-documents.md`).
- **Output delivery and notification.** Settled: notification is publish/subscribe over the event sink plus
  record-based catch-up, and there are no webhooks and no push-to-sink delivery of outputs in the surface
  (`runtime-api/1-overview-and-state.md § Notification is publish/subscribe, not webhooks`,
  `runtime-api/3-observation-and-notifications.md`); a consumer reads a run's outputs back from the record
  (`runtime-api/4-run-record-read-model.md`). This was verified against the live runtime and middleware, which
  expose Pub/Sub publishing and record-based catch-up, never webhook delivery.
- **Cost attribution below the run.** Settled: cost is attributed per run, per phase, per task, and per judgment
  and read from the record (`runtime-api/4-run-record-read-model.md § Recorded cost and metrics`); a per-requester
  or per-tenant key is out of scope as the multi-tenant trust boundary
  (`4-limits-and-non-promises.md § Out of scope entirely`).
