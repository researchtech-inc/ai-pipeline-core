# Operating a run from outside the process

This is the entry point to the runtime API: the contract for operating a run from outside the process that executes
it. An external driving system and its operators launch a run, observe it while it runs, halt it, recover it, and
read back its results, logs, record, and cost — all over a boundary they did not author, through supported framework
surfaces rather than by reaching into internals. This subtree is the out-of-process counterpart to the in-process
programmatic surface in `advanced-api/`: where `advanced-api/` is imported and called inside the process that drives
a run, the runtime API is consumed by a system that cannot import the application's or the framework's Python
definitions at all.

The per-capability contracts live in the files beside this one: launching and controlling a run
(`runtime-api/2-run-control.md`), observing it and being notified of its lifecycle
(`runtime-api/3-observation-and-notifications.md`), reading its execution record and cost
(`runtime-api/4-run-record-read-model.md`), reading its documents and logs (`runtime-api/5-documents-and-logs.md`),
and scheduling child pipelines that run the same way whether local or remote (`runtime-api/6-child-pipelines.md`).

This surface is written for the external driving system and its operators — never the authoring agent. The
operator-command face of it — `ai-pipeline cancel`, `ai-pipeline recover`, and `ai-pipeline inspect` operating
across a boundary — is in `tools/`, and shares these guarantees. The runtime this surface operates against is the
same one whether it is stood up where the work is developed (the local full-stack profile, `5-configuration.md`) or
in production; there is no separate local-only mode, so the shapes and examples below apply identically to a local
runtime and a remote one.

## What this contract pins, and what stays mechanism

This subtree commits to *what* a consumer can do and to the shapes it constructs and parses. Two things are
contract. First, the framework's own vocabulary: pipeline runs, phases, tasks, judgments, run identity, run state,
documents, logs, and cost. Second, the **pinned, versioned interop shapes** an integrator constructs and parses —
the lifecycle event envelope (`runtime-api/3-observation-and-notifications.md`), the document-input and launch shapes
(`runtime-api/2-run-control.md`), and the run-state, record, and cost shapes
(`runtime-api/4-run-record-read-model.md`, `runtime-api/5-documents-and-logs.md`). Those are shown concretely, the
way `api/` shows class shapes, because an external consumer cannot import them.

Two presentation conventions carry those shapes, and a reader uses the right one per block. The control-plane and
lifecycle-event blocks (`runtime-api/2-run-control.md`, `runtime-api/3-observation-and-notifications.md`) show the
real wire keys, which an integrator builds and reads as written. The read-model blocks
(`runtime-api/4-run-record-read-model.md`, `runtime-api/5-documents-and-logs.md`) show each field under its 0.3.0
name and annotate the real wire spelling alongside wherever it differs (for example `flow_name` for `pipeline_name`,
`span_type` for `kind`, `document_sha256` for `document_id`); the annotated wire spelling is the key on the boundary.

Each capability is shown as a concrete endpoint reference with worked request → response and error examples, the
way `api/` shows class shapes and examples, because the consumer reads only this layer. The request, response,
event, and error **shapes** are the break-sensitive contract. The HTTP method and path shown alongside each
capability are the **binding the supported runtime ships** — concrete and usable as written — rather than a frozen
contract independent of that runtime: the specific method+path strings are the current supported binding and may
change while the shapes stay stable, so an integrator relies on the shapes as contract and reads the method+path as
the binding the runtime exposes today (`_decisions.md § The runtime interop shapes are contract; the HTTP
method+path are the supported binding`). The shapes are what a consumer constructs and parses; the binding is how
the supported runtime exposes them.

What stays mechanism is what genuinely varies underneath without changing the shapes a consumer relies on: the
transport's status-code mapping, the ports, the specific event-bus and orchestrator products, the server framework,
and the authentication and authorization layer in front of the surface. The wire encoding that carries the shapes is
mechanism, stated once here and not repeated per file.

The trust boundary is out of scope: who may operate a run, how the surface is exposed to outside callers, and the
multi-tenant boundary stay at the platform edge (`4-limits-and-non-promises.md § Out of scope entirely`).

## The externally readable record is versioned and self-describing

Everything a consumer reads through this surface — the record, the delivered results, the documents that cross the
boundary, the run's recorded events — is versioned and decodable without the producer's code. A reader that meets a
shape it does not understand fails loudly with a version mismatch rather than silently mis-decoding
(`3-guarantees.md § Externally readable, versioned record`). The read models in `runtime-api/4-run-record-read-model.md`
and `runtime-api/5-documents-and-logs.md` are the committed projection; nothing about deeper internal shapes is
promised to outside readers.

## One run identity addresses a run across every surface

A run has one stable identity that addresses it coherently wherever it is launched, observed, halted, recovered, or
read (`3-guarantees.md § One run identity across every surface and over time`). The same identity used to launch a
run in `runtime-api/2-run-control.md` correlates its events in `runtime-api/3-observation-and-notifications.md`, its
record in `runtime-api/4-run-record-read-model.md`, and its documents and logs in `runtime-api/5-documents-and-logs.md`.
A consumer never invents its own correlation scheme.

### Constraints

- Run identity is an opaque stable string the consumer supplies at launch or the framework assigns; the consumer
  does not parse structure out of it.
- The same identity may be reused across attempts over time. A reused identity stays disambiguable by a recorded
  attempt reference point — the recorded start of the execution attempt — so a read, an observation, or a recovery
  resolves the intended attempt rather than silently colliding with an earlier one.
- A child pipeline's run is correlated to its parent through the parent run's root identity carried on every record
  and event (`runtime-api/6-child-pipelines.md`); the consumer addresses child and root records without a bespoke
  scheme.
- A consumer may also attach optional correlation labels at launch (`runtime-api/2-run-control.md § Correlation
  labels are immutable run metadata`) to tag a run with its own external dimensions. Labels are record metadata that
  refine correlation; they never replace the run identity, which remains the one handle that addresses the run.

## The observed run-state is one shared type

A run's observed state is one shared, broader type — `RunState` — used everywhere a run's state is read, reconciled,
observed live, or notified. It is not a separate external state model; it is the one run-state vocabulary. The
in-process synchronous result (`advanced-api/1-runners-and-clients.md`) reports the outcomes a blocking call can
observe, which overlap `RunState` on `COMPLETED` and `FAILED`; the watcher-only members below are read only from
outside the executing process.

### Reference

`RunState` is a closed set:

- `PENDING` — the run is accepted and queued, not yet executing.
- `RUNNING` — the run is executing.
- `COMPLETED` — the run finished and delivered its result.
- `FAILED` — the run stopped on a terminal failure, with a recorded reason.
- `CANCELLED` — the run was halted from outside before it completed and ended as a recorded terminal state
  (`runtime-api/2-run-control.md`).
- `CRASHED` — the run's execution or observation was lost without a recorded terminal transition, resolved by
  reconciliation against the record rather than inferred from silence or elapsed time
  (`3-guarantees.md § Reconcile to recorded truth`).

### Constraints

- `RunState` is the single observed run-state vocabulary across the read models, reconciliation, and the live
  lifecycle events; a consumer branches on its members and does not invent its own state names. There is no separate
  "recorded status" vocabulary at the run level: a raw record read (a run summary or list read) returns the recorded
  `RunState` — one of `PENDING`, `RUNNING`, `COMPLETED`, `FAILED`, `CANCELLED` — and the reconcile read
  (`runtime-api/2-run-control.md`) is the only producer of `CRASHED`. `cached` and `skipped` are unit-level span
  statuses (`runtime-api/4-run-record-read-model.md`), not run states.
- The durable record is authoritative over any live signal. A consumer reconciles and deduplicates the live state
  against the record, which is the account of what actually happened (`3-guarantees.md § Live observation with
  authoritative recovery`).
- `CRASHED` is established by reconciliation, never by a watcher concluding from a missing heartbeat or elapsed time
  that a run died; a quiet `RUNNING` run and a `CRASHED` one are distinguished only against the record
  (`runtime-api/2-run-control.md`).
- The in-process blocking result reports only `COMPLETED`, `FAILED`, or `INCOMPLETE`
  (`advanced-api/1-runners-and-clients.md`); `INCOMPLETE` is the blocking-call name for a run that returned before
  reaching a terminal `RunState`. The observed members `PENDING`, `RUNNING`, `CANCELLED`, and `CRASHED` are read only
  from outside the executing process.

## Notification is publish/subscribe, not webhooks

The framework publishes a run's lifecycle to an event sink that observers subscribe to, and lets a consumer catch up
on what it missed by reading the durable record. There is no webhook, no callback URL, and no push-to-sink delivery
of outputs; a consumer reads a run's result back from the record. The event shape and catch-up semantics are in
`runtime-api/3-observation-and-notifications.md`.

## The framework stands up the runtime that executes and exposes a run

The runtime this surface operates against is the framework's supported, versioned topology
(`3-guarantees.md § A supported runtime`), stood up by `ai-pipeline deploy` (`tools/11-deploy.md`) and configured
through the event sink and orchestrator/runtime backend categories (`5-configuration.md`).
