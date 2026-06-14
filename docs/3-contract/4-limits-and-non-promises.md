# Limits and Non-Promises

The framework is intentionally bounded. These boundaries are part of the contract: they protect you from assuming
the framework does things it does not do. This file is the other half of `3-guarantees.md`. The guarantees are
real and worth relying on; every one of them is about the *mechanics* of the work — structure, recording,
recovery, reproducibility — and none of them is about whether an analysis is correct.

## Structural soundness is not analytical correctness

The framework makes a run traceable, typed, validated, bounded, and reproducible. It cannot make a conclusion true.
A run can be impeccably recorded and systematically wrong, and that is the most expensive failure in this class of
system, because everything around the wrong answer checks out. A perfectly traceable wrong answer is still wrong.

Nothing in the framework certifies that an analysis is correct, and nothing in it should be read as evidence that
the analysis is correct. What structure buys is named honestly: diagnosis, repair, audit, and recovery — and
nothing more. Treating clean structure, passing validation, or a complete record as a substitute for reviewing the
analysis relaxes review at exactly the point where it must not relax.

## The bright line: generic operational complexity is the framework's; domain truth is the application's

The framework owns the generic operational complexity every long-chain analytical application shares — persistence,
provenance, retries, pacing, validation loops, caching, recording, replay, and the runtime that launches, executes,
observes, halts, recovers, and exposes a run. It does not own anything domain-specific
about whether the analysis is any good. On the application's side of the line:

- **Evidence reliability** — whether a source is current, independent, or about the intended subject.
- **Source independence** — whether agreeing sources corroborate or merely echo one upstream claim.
- **Contradiction and ambiguity policy** — when a provisional conclusion may harden, and what an unresolved state
  means.
- **Methodology** — evaluation rubrics, source-reliability policy, and how a judgment is operationalized.
- **Scoring and thresholds** — what a score means and where a decision boundary sits.

The framework gives the substrate to express these honestly — typed outcomes, document-backed citations,
abstention as a valid response, preserved ambiguity — but the epistemics are the application's. The framework does
not supply domain methodology, and an application that needs it supplies its own.

## The framework rejecting your shape is the product working

There is one correct shape per intent, and the framework rejects the others — at import time, at plan-compile time,
or at a typed boundary. A rejected declaration, a refused selector, an unbound collection, a mismatched role
suffix: these are the framework forcing a correct shape, not defects to work around. When the framework refuses
what you wrote, the response is to write the shape it accepts, not to find a way past the check. The strictness is
the mechanism by which machine-speed authorship stays correct; loosening it to accept a convenient shape removes
the guarantee it was protecting.

## What is not promised

- **Exhaustive discovery.** The framework does not promise that all relevant evidence was found or that an
  application examined everything in scope. Absence of a finding is not proof of absence in the world.
- **Cross-run score comparability.** Scores, rankings, and judgments are meaningful within the run that produced
  them under the conditions of that run. The framework does not promise they are calibrated or comparable across
  runs, models, or time.
- **Model and provider behavior stability.** The execution substrate is volatile: models differ from one another
  and from their own past behavior, and a provider can change beneath a running system. The framework keeps an
  application *operable* through that drift; it does not freeze the substrate or promise that a judgment made today
  reproduces a judgment made earlier.
- **One-shot correctness of model output.** The framework validates response shape and runs a bounded repair loop;
  it does not promise the model's first response, or its repaired response, is analytically right. A valid response
  is well-formed, not necessarily true.
- **Replay as bit-identical reproduction.** Replay re-executes deterministic code faithfully and re-samples model
  judgments against recorded inputs. It reproduces the *mechanics* of a run, not the exact tokens a model produced;
  a replayed model round may differ from the original, and that difference is what replay exists to measure, not a
  defect.
- **Exactly-once live delivery.** The live observation signal is at-least-once, not exactly-once, and it is not the
  source of truth. A consumer reconciles and deduplicates against the durable record, which is authoritative;
  building on an assumption that every event arrives exactly once and in order is unsupported.
- **Zero-loss logging.** Logs are durably recorded and near-complete, but a hard process kill can drop the very
  tail of the live log stream. The durable record, not the last buffered log line, is authoritative.
- **Instantaneous cancellation.** A halt stops further work and spending as soon as the framework can act on it;
  work already in flight at the moment of the signal may complete and be recorded. Halt is prompt and clean, not
  instantaneous.
- **A latency or throughput guarantee.** The framework keeps the operate-and-launch path simple, uniform, and free
  of avoidable overhead — non-blocking launch, prepared deployment, content-addressed reuse — but it does not
  promise a specific time-to-start, run duration, or throughput. Cold starts, scheduling, the work itself, the
  models, and where the runtime is deployed bound real-world speed. "Fast" is a structural promise about removing
  avoidable overhead, not a service-level commitment to a wall-clock number.
- **Capacity parity between local and remote.** Operation is identical across placements (`3-guarantees.md §
  Operational parity across placements`), but a single local process does not match a distributed runtime's
  throughput, concurrency, or scale. The promise is the same *operation and behavior*, not the same capacity.
- **Webhook or push delivery.** A run's lifecycle is published to an event sink that observers subscribe to, and a
  consumer reads results and missed events back from the record; the framework does not call back into a
  consumer-supplied endpoint and does not push outputs to a sink. There are no webhooks and no callback URLs in this
  surface (`runtime-api/3-observation-and-notifications.md`).

## One supported way per concern

The framework is not broadly configurable or pluggable. For each generic operational concern — persistence, the
orchestrator/runtime backend, the event/notification transport, the deploy target, the run-log store — it provides
one supported way, selected by configuration but not offered as a menu of interchangeable mechanisms. The single
exception is the model gateway, where model selection may target different providers through `AIModelRef`, because
several providers occupy one role the author and operator already meet as a single shape.

This strictness is deliberate, and it is what keeps the surface small and turnkey: every interchangeable option is
another visible shape the next agent copies and another axis along which authored and operated systems diverge;
supporting many alternatives multiplies the machinery the framework must keep correct; and one chosen way can be
made fast to stand up where a menu cannot. The backends behind these concerns do not surface in the authoring
surface — an author neither names nor configures whether the runtime uses one orchestrator or another (`api/`,
`5-configuration.md`). An application whose needs genuinely differ builds its own alternative outside the framework;
it does not ask the framework to grow a second interchangeable way.

## Out of scope entirely

The framework is the substrate and the runtime that executes a single run; it is not the products built across many
runs or in front of one. Operating, observing, halting, recovering, and reading back a single run — and standing up
the runtime that executes it — are in scope. What stays out:

- **A stateful cross-run platform** — persistent workspaces, invalidation cascades, scoring engines, verification
  queues, living assessments that accumulate, score, or reason over state across many runs. The line is the run:
  per-run operation is the framework's; cross-run accumulation is a product that consumes it.
- **A user interface or dashboard product** — the framework owns the data and the observable account of a run;
  rendering and the human-facing application are a separate product.
- **An analytics or reporting warehouse over the run record** — the record operates one run and traces one
  conclusion, not arbitrary cross-run analytical questions. The record is a benchmark substrate a consumer reads to
  compare and score runs — the framework preserves each run's inputs and outputs and re-executes a unit so a
  consumer can measure — but performing the comparison, scoring the results, and deciding from them belong to the
  consumer, not the framework.
- **The authentication, authorization, or trust boundary** — who may operate a run, how the surface is exposed to
  outside callers, and the multi-tenant boundary stay at the platform edge.
- **A generic workflow engine, a chat or assistant platform, or a domain-methodology system** — generality and
  domain epistemics are out for the reasons named throughout this file.

Absorbing any of these would couple every application to one downstream product's epistemics or one operator's
exposure model. The framework offers the substrate such systems need — durable records, stable identity,
recoverable history, and a versioned externally readable surface — for any consumer to build on.
