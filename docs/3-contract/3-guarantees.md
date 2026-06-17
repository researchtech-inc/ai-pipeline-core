# Framework Guarantees

These are the things the framework owns so that no application rebuilds them. Each is something you may rely on
across every application; none of them is something you implement, instrument, or work around. This file states
what you can trust, not how it is achieved — the mechanisms are framework internals. Where a guarantee has a sharp
edge, this file names the edge and points to `4-limits-and-non-promises.md`, which is the other half of this one:
a guarantee about mechanics is never a guarantee about the truth of an analysis.

The guarantees below are stated once here. The `api/` files state only the local consequence of each at the point
an author meets it; this file is the authoritative list.

## Durable record

Every document the application creates through the contract surface, and every run — together with the run's
lifecycle events and execution logs — is persisted. Nothing an application produces through a document factory or
returns from a task can be silently lost between steps, across a failure, or after the run ends. The record
outlives the process that produced it and is available for inspection, replay, and audit. You do not write
persistence code, and you do not decide what to keep — the durable record is complete by default.

The record is append-only and additive: it preserves every operation as durable history and never replaces or
deletes what it already recorded. A later revision, summary, or correction is recorded as a new entry beside the
prior one, not written over it, so the account of what was true earlier stays recoverable after later work changes
the picture — this is the store-wide form of _Identity and immutability_ below. Old history may be archived or
compressed to reclaim space, but the recorded facts are never discarded; compression preserves them, it does not
summarize them away. Because the record is the authoritative account of what a run did, what is not recorded did
not happen for any later reader: there is no supported path by which work proceeds on state held only in process
memory and never written down.

## Recording halts the run when it cannot proceed

Because the durable record is the authoritative account of a run, the framework never advances a run while it
cannot record what that run is doing. A transient problem reaching the store is the framework's to absorb through
its own retry and recovery mechanics; an unrecoverable one halts the run rather than letting it continue on
authoritative state that exists only in process memory. This is stronger than an ordinary task failure, which
stops one unit of work: a recording failure the framework cannot recover from stops the whole run, because no part
of it can be trusted once the account of what happened is no longer being written. A run halted this way resolves
against what was actually recorded, the same way a crashed run does (_Reconcile to recorded truth_ below); the
framework does not invent a terminal state it could not record. This is a framework-and-runtime condition, not an
application concern — application code does not detect store failures, retry around them, or decide to continue
without the record.

## Automatic provenance

Every non-root document records its immediate upstream documents — the content that contributed to it, the work
that caused it, the external source it came from, or the prior revision it supersedes — with no instrumentation
from the application. Prompt inputs and the documents a response cites are recorded as sources automatically. From
any document, the record can be walked back to the evidence and judgments that produced it and forward to
everything that consumed it. Provenance is recorded faithfully; it makes a conclusion explainable, not correct.

## Identity and immutability

Documents are immutable after creation, and identity is framework-owned and stable. The same document resolves to
the same identity within a run and across runs, so references, citations, and rehydrated inputs stay valid over
time. An application never constructs identity, mutates a document, or copies content to "import" it; revisions
create new identities while preserving the prior version.

## Pre-run validation

A pipeline that cannot run does not start. Unsatisfied inputs, incompatible outputs, invalid selectors, unbounded
repetition, undeliverable results, and malformed declarations are rejected before any task executes — at import
time or at plan-compile time. A run begins only once the framework has proved its declared shape is executable. The
configuration-independent shape is provable without a `RunConfig` (`ai-pipeline verify`); bounds and values that
depend on a concrete `RunConfig` — a loop or fan-out bound supplied as a `RunConfig` field — are proved against the
actual configuration at plan-compile (`ai-pipeline plan`) and run start. The diagnostics name what is wrong; the
development loop (`6-tools-and-the-development-loop.md`) lets you surface them without spending a run.

## Bounded execution

Every repeated or iterative construct carries a declared maximum — fan-out item counts, fold counts, loop rounds,
per-tool call budgets — and the framework enforces it. A collection that exceeds its bound is rejected explicitly;
the framework never silently truncates, samples, or drops work to fit a limit. Because the bounds are declared and
enforced, the maximum cost and fan-out of a run are knowable before it starts, and machine-speed execution cannot
run away unbounded.

## Failure handling and retry

Failure has one typed vocabulary at every boundary — task, tool, service client, and prompt execution: a retryable
failure leaves work eligible for framework-owned retry; a terminal failure stops the work because the inputs or
environment require an external fix. The framework owns retry policy, pacing against external limits, and timeouts.
Application code does not sleep, poll in loops, throttle, or implement its own retry around framework calls; it
raises the typed failure and the framework decides.

## Validation and repair

A prompt contract's `validate` result drives a framework-owned repair loop: a rejected response is sent back for
repair within a bounded budget, and exhaustion of that budget is a terminal failure, never a fabricated success.
The application declares what an acceptable response is; the framework runs the loop and refuses to manufacture an
answer when the model cannot produce a valid one. A well-formed abstention is an accepted response, not a failure
the loop tries to overturn.

## Model routing, caching, and concurrency

Model selection, provider routing, transport, response caching, and the concurrency of independent work are owned
by the framework and opaque to the application. An application passes an `AIModelRef` and relies on the judgment
being executed; it does not choose a provider, manage a connection, tune caching, or schedule parallelism. Task
results are cached by content, so a rerun after a fix reuses already-correct work instead of repeating the whole
run; the per-run cache policy is `CachePolicy` (`advanced-api/1-runners-and-clients.md`), and cache hits are
visible in the record.

## Location independence

A run behaves identically whether it executes in one process or across many, and inspection or recovery can happen
elsewhere and later than execution. The framework owns where and when each task runs; an application never assumes
that two tasks share a process, that a later task sees memory an earlier one left behind, or that the process that
produced a document is the one that reads it. Everything a task needs arrives as its resolved document inputs and
its declared fields, and everything it contributes leaves as returned documents and the recorded run — so a task is
correct wherever it runs, with the same output identity, provenance, cache identity, and replayability. Correctness
never depends on which process held which local memory; relocation, resume after a crash, and later audit all see
the same authoritative record.

## Operational parity across placements

The same application is operated identically whether it runs in one local process for development or across many for
production: the same commands, the same runtime surface, the same record, and the same inspect, replay, halt, and
recover behavior apply in both. Local in-process execution requires no external infrastructure; targeting production
is a configuration change, not a different tool, a different command, or a different mental model. This is the
operator-facing twin of _Location independence_ above — that guarantees the same *answer* across placements; this
guarantees the same *operation* — so what you verify where the work is developed is what ships. Parity is
operational and behavioral, not a promise of identical throughput or capacity (`4-limits-and-non-promises.md`).

## Non-blocking launch and prepared operation

Launching a run *from outside the process that executes it* — the runtime launch surface
(`runtime-api/2-run-control.md`) — returns promptly with the run's identity and does not block until the run
completes; a run is observable from the moment it is accepted, not only after it ends. (The in-process `Runner.run`
(`advanced-api/1-runners-and-clients.md`) is a blocking call by design: it returns the recorded result when the run
completes, fails, or stops incomplete; the operator command `ai-pipeline run --remote` likewise follows a run to its
terminal state and returns its outputs, so the operator loop is identical across placements even though the launch
beneath it is non-blocking. All three produce the identical record, differing only in calling convention.) A
deployed pipeline accepts a run promptly, without a per-run rebuild — deploy once, launch many. Speed comes from prepared operation and from reusing
already-done work, never from skipping the record: the unchanged majority of a long run is reusable, so correcting
or re-measuring one part costs the work of that part rather than a re-execution of everything around it. The
framework promises no specific wall-clock latency or throughput; cold starts, scheduling, and where the runtime is
deployed bound real-world speed (`4-limits-and-non-promises.md`).

## Cost recording and attribution

Every run records what it spent. Cost is attributed to the units that incurred it — down to individual judgments —
and aggregated per task, per phase, and per run, so spending is bounded by the declared run shape and attributable
after the fact. An operator reads recorded cost from the run record and can preview the maximum fan-out and cost of
a run before starting it (`6-tools-and-the-development-loop.md`). Cost is recorded in the run record, not exposed on
authoring result objects; a `PromptResult`, for example, carries the response and its citations, not cost or
transport metadata.

## Preserved, traceable, replayable runs

The record a run leaves behind is sufficient on its own to do three things without rerunning the work blindly:
trace any delivered conclusion back through its intermediate judgments to the evidence that produced it; re-execute
a unit against its recorded inputs to study or measure it; and resume an interrupted run from where it stopped
without recomputing completed work or changing the meaning of prior results. The record is consumable in bounded
portions, which matters because a production run leaves more behind than any reader consumes at once. Replay
re-executes deterministic code faithfully and re-samples model judgments; it is not a claim of bit-identical model
output (`4-limits-and-non-promises.md`).

Because every run durably records its inputs and its outputs — not merely that it ran — the accumulated record of
past runs, including production runs, is a reusable benchmark and regression substrate: a unit or a whole run can be
re-executed against recorded inputs under a different model or judgment and compared to the recorded baseline, with
cost attributed down to the judgment. The framework provides that substrate — durable inputs and outputs, unit
re-execution, attributed cost. The methodology that compares runs, and the judgment of which result is better,
belong to the consumer reading the record, not to the framework (`4-limits-and-non-promises.md`).

## Live observation with authoritative recovery

A run is observable while it executes, not only after it ends. The live signal is best-effort and may be
incomplete — a watcher connects late, drops, and reconnects — but the durable record is the authoritative account
of what happened, and a watcher that missed a stretch of a run can recover exactly that stretch from the record.
Every observable transition is recorded independent of whether live delivery succeeded, so a degraded or absent
live channel never costs observability, and the live and recorded views carry a stable identity that lets a
consumer merge them without duplication. A consumer may observe a run by subscribing to the live signal or by
polling the record for what it missed; both are first-class and both reconcile to the same authoritative record, so
neither subscription nor polling is the privileged path. The live channel is at-least-once, not exactly-once
(`4-limits-and-non-promises.md`).

## Governable in-flight runs

A run launched from outside can be halted from outside before it completes, without killing the process that runs
it and without corrupting what was already recorded. A halted run ends as a recorded terminal state, not as a
crash, so what it produced before it was stopped stays intact and readable. Halt is prompt and clean, not
instantaneous: work already in flight when the signal arrives may finish and be recorded
(`4-limits-and-non-promises.md`).

## Reconcile to recorded truth

A crashed or interrupted run's authoritative state is reconciled against the record, not inferred from the last
observed signal or from elapsed time. Recovery resolves a run to what was actually recorded, so a crashed run and
a quiet one are distinguishable, and a dropped or stale final signal does not become a confident wrong answer about
whether the run finished or how it ended.

## Externally readable, versioned record

A run's record, its results, and its documents can be read by a system that did not produce them and cannot import
their definitions. The externally readable surface is versioned and self-describing; a reader that meets a shape it
does not understand fails loudly with a version mismatch rather than silently mis-decoding. This covers the surface
external systems consume — the record, the delivered results, the documents that cross a boundary, and the run's
recorded events — and nothing about deeper internal shapes is promised to outside readers.

## One run identity across every surface and over time

A run has one stable identity that addresses it coherently wherever it is launched, observed, halted, recovered, or
read, and that stays disambiguable when the same identity is reused across attempts over time. An integrator
correlates a run across every surface by that one identity and never invents its own naming scheme. This is run
identity; document identity is the separate guarantee above (_Identity and immutability_).

## Preserved consumer correlation metadata

A consumer may attach immutable correlation labels to a run at launch, and the framework preserves them on the run
record and across the observation surfaces — the run summary, the lifecycle events, and a nested child run — so an
external system can correlate framework runs back to its own dimensions (an entity, an experiment, a batch) without
overloading the run identity or maintaining a private side-table. Labels are recorded once and never reinterpreted:
the framework stores, propagates, and equality-filters them, but reads no meaning from them. They are operational
correlation metadata, distinct from provenance, from document and run identity, and from authored configuration. A
run's labeled outputs are found by reading the runs that carry the label and the documents they produced — never
from a label on the document itself — so a consumer's index over framework work stays a rebuildable projection of
the record rather than a separate source of truth. Filtering is exact-equality listing only, not an analytics query
surface (`4-limits-and-non-promises.md`).

## Durable, readable logs

A run is understandable from one diagnostic account. The work of a single run is carried out across many cooperating
parts — the machine it runs on, the machinery that schedules and executes it, the surface through which it is
operated and observed, the application's own steps, and the external services it called — and the diagnostic output
of every one of those parts is gathered into one run-correlated account, durably readable by run and by unit, with
level and category filters. A diagnosing agent reads that account in one place rather than finding, reaching, and
reconciling a separate, differently-shaped record for each part. Logs are part of the supported record an agent and
an external operator read to diagnose and recover a run — not incidental output that disappears with the process.
The one edge — that a hard kill can drop the very tail of the live log stream — is named in
`4-limits-and-non-promises.md`. How the accounts of the cooperating parts are unified and stored is a framework
internal.

## A supported runtime

The framework ships a maintained, versioned way to stand up the runtime that executes runs — the cooperating
services a production run needs — as one verified topology, so each adopter does not reassemble it by hand. It names
which surfaces are exposed and which are sealed. It does not promise host networking, external exposure,
autoscaling, or the trust boundary; those stay at the platform edge (`4-limits-and-non-promises.md`).
