# Configuration

Configuration is the surface where you tell the framework which run-varying values and which environment it
operates in. There are exactly two channels: `RunConfig`, which carries values that vary per run, and environment
configuration, which carries process-level settings the framework reads on its own. Everything else — how the
framework routes, retries, caches, paces, persists, and records — is owned by the framework and is not
configurable.

What this file commits to as contract is the two channels and the *categories* of configuration they cover. The
concrete environment-variable names and backend families it shows appear only as examples that illustrate a
category; a deployment may spell a key differently or select a different backend without changing what an
application relies on. Every other file states behavior, not backends.

## RunConfig is the only run-varying channel

`RunConfig` is the configuration contract the runner supplies when a run starts. It is how a run is parameterized:
model selection, bounded-work limits, and any other value that changes from one run to the next enters here and
nowhere else. The pipeline reads `RunConfig` during plan construction and passes selected values into phases as
fields; tasks receive those values through phase planning. The full authoring contract for `RunConfig` is in
`api/8-pipelines.md § RunConfig defines runner-supplied configuration`.

### Constraints

- Run-varying values reach the application only through `RunConfig`; application code does not read environment
  variables, parse model strings, or construct configuration inside pipeline, phase, or task code.
- A pipeline passes concrete `RunConfig` values into phase constructors; it does not hand the whole configuration
  object to a phase to hide that phase's real configuration needs.
- A run's bounds (loop rounds, fan-out limits) come from the business specification, a static constant, or a
  `RunConfig` field — never from runtime document content.

## AIModelRef is an opaque handle

Model selection enters as an `AIModelRef`. You construct it once at the configuration boundary from a model name
and its capability flags, supply it through `RunConfig`, and pass it down to the judgments that use it. It is
opaque: application code passes it to `.execute(model=...)` and does not inspect, parse, compare, branch on, or
pickle it. Which provider serves it, how it is routed, and how responses are cached are framework concerns behind
the handle, not application configuration.

The capability flags you set when constructing an `AIModelRef` (for example, `supports_url_substitution`) describe
the model to the framework; they are construction-time configuration, not runtime inspection. Model tiering — which
model a given judgment uses — is a design decision expressed through `RunConfig`, covered in
`authoring/4-planning-model-interactions.md`.

## Environment configuration

The framework reads process-level settings from the environment so that credentials, backend selection, and
operational knobs stay out of application code. An operating agent sets these before a run; an application never
reads them. The contract is the set of categories below; the keys and backends named are illustrative.

- **Model gateway.** The endpoint and credentials for the model provider the framework calls — illustrated by
  `OPENAI_BASE_URL` and `OPENAI_API_KEY` for an OpenAI-compatible gateway. The application names models by
  `AIModelRef`; the gateway is configured here.
- **Persistence backend.** Which durable store backs runs and documents. There are three supported profiles of
  this one concern: an in-memory store for tests, which keeps the same append-only behavior for the lifetime of the
  test process and is not durable after it exits; local filesystem storage, the default for development, which
  needs no external service; and a shared backend store for distributed execution (currently ClickHouse, though the
  backend identity is illustrative and not guaranteed). The recording semantics the application relies on —
  append-only, traceable, replayable records — are identical across all three; only this configuration selects
  which one. Durability is the one property that differs by profile: the filesystem and shared-backend stores
  provide the post-process durable record the _Durable record_ guarantee describes (`3-guarantees.md`), while the
  test-only in-memory store preserves the same append-only semantics for the lifetime of the test process but is
  not durable after it exits. These are profiles behind one persistence concern, not a menu of interchangeable
  mechanisms the adopter assembles (`4-limits-and-non-promises.md § One supported way per concern`).
- **Observability sink.** Where execution records and error reports are emitted, when an external sink is
  configured (illustrated by a `SENTRY_DSN`). With no sink configured, the run still records to the durable store.
- **Run-log / diagnostics store.** Where the run's unified diagnostic account — the logs of every cooperating part,
  correlated to the run (`3-guarantees.md § Durable, readable logs`) — is collected and read back from. It is
  distinct from the observability sink above: that carries error reports to an external monitor, while this is the
  durable, queryable log account an agent reads to diagnose and recover a run. With no separate store configured,
  the account lives in the durable record alongside the run.
- **Event sink.** Where live run-lifecycle events are published for external observers, when an event bus is
  configured (illustrated by a Pub/Sub project and topic). It is distinct from the observability sink above: the
  event sink carries the observable lifecycle of a run, not error reports. With no event sink configured, a run's
  lifecycle remains reconstructable from the durable record.
- **Orchestrator / runtime backend.** Which execution backend runs and schedules work across processes and machines
  (illustrated by a Prefect-family backend and its connection settings). Local single-process execution is the
  default and needs no external orchestrator; a distributed runtime is selected by configuring its backend. The
  behavior the application relies on is identical across backends; only this configuration selects which one.
- **Operational limits.** Framework-owned budgets exposed as settings — for example the prompt-contract repair
  budget (illustrated by `PROMPT_CONTRACT_MAX_REPAIR`, default `2`) and the framework's retry budgets (illustrated
  by `TASK_RETRIES`). These are process-wide budgets, not per-call parameters and not per-call policy, and they are
  not set from application code.

### Constraints

- The contract commits to these categories and to the two channels, not to the spelling of any environment-variable
  name or the identity of any backend; those are illustrative and may differ by deployment.
- Backends and external services are selected by environment configuration, never by application code or by a
  `RunConfig` field.
- The backends behind these categories — model gateway, persistence, observability sink, event sink, orchestrator,
  run-log store — sit behind their boundaries, so selecting the configured backend for a deployment (for example
  local filesystem versus a shared store) does not change what the application relies on or how it is authored. This
  is configuration selecting the supported backend for a profile, not an open menu the adopter assembles — the
  next bullet states that limit.
- The framework provides **one supported way per concern**: each category selects one configured backend, not a
  menu of interchangeable mechanisms the adopter wires together. The single exception is the model gateway, where
  model selection may target different providers through `AIModelRef`. This is the configuration face of
  `4-limits-and-non-promises.md § One supported way per concern`.
- The chosen backends never surface in the authoring surface: an author neither names nor configures whether the
  runtime uses one orchestrator, store, or transport over another, and `api/` exposes none of these (`api/`).
- An operating agent supplies these settings to the process; the application reads none of them.

## Local and production are one configuration surface

There are two local profiles, and moving between local and production is a configuration change, not a different
application shape (`3-guarantees.md § Operational parity across placements`):

- **In-process, zero infrastructure.** With persistence defaulting to local filesystem storage and execution
  defaulting to local single-process, no external orchestrator, event sink, or run-log store is required;
  `verify`/`plan`/`run`/`test`/`replay`/`inspect` operate entirely in-process and still produce the same durable
  record a full run produces.
- **Local full stack.** Selecting the distributed persistence, orchestrator, and event-sink backends — pointed at a
  supported runtime stood up where the work is developed — gives full runtime/API parity locally: the same surface,
  operated the same way, as production. Standing the stack up is the deploy step (`tools/11-deploy.md`); there is no
  separate `serve` mode.

The same application is authored once and run under either profile unchanged; only this configuration selects
which.

## What is not configurable

The framework owns the behavior that makes its guarantees hold, and that behavior is not exposed as configuration:

- Retry *policy and mechanics* — when and how the framework retries, paces against external limits, and times out.
  (A bounded retry *budget* is a process setting under Operational limits above; the policy that consumes it is not
  tunable per call.)
- The validation-repair loop's structure (its budget is a process setting; its behavior is not tunable per call).
- Cache key derivation and cache semantics (a run selects a cache *policy* — `CachePolicy` in
  `advanced-api/1-runners-and-clients.md` — but not how caching works).
- Model routing, provider selection, and transport behind an `AIModelRef`.
- How provenance, identity, persistence, and recording are performed.

These are framework internals. An application that needs to change one of them is reaching past the contract; the
correct response is to treat the need as a framework change, not to expose a new knob.
