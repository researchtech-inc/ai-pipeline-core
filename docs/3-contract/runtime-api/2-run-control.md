# Controlling a run from outside

This file defines the control plane of the runtime API: discovering what can be run, launching a run with its
inputs, halting a run in flight, and reconciling a crashed or interrupted run to its recorded truth — all by run
identity, from outside the process that executes the run. Reading what a run produced is the read plane
(`runtime-api/4-run-record-read-model.md` and `runtime-api/5-documents-and-logs.md`); observing it live is
`runtime-api/3-observation-and-notifications.md`.

## Discovering what can be launched

Before launching, a consumer can discover the deployed pipelines available to it and the shape each one accepts, so
it can build a valid launch without importing the pipeline's code.

### Capabilities

- **Runtime health.** A consumer can check that the runtime is reachable and able to accept work.
- **List runnable pipelines.** A consumer can enumerate the deployed pipelines it may launch.
- **Read one pipeline's launch contract.** For a deployed pipeline, a consumer can read its self-describing
  integration descriptor: the root document types it accepts, the document-input schema, the run-configuration
  schema, the result schema it delivers, and its declared phase chain.

### Reference

The integration descriptor a consumer reads for one pipeline:

```text
integration_descriptor = {
    "deployment_name": str,
    "input_document_types": [{"class_name": str, "description": str}, ...],
    "document_input_schema": {...},        # JSON Schema for one root input object
    "options_schema": {...},               # JSON Schema for the run configuration
    "result_schema": {...} | None,         # JSON Schema for the delivered result
    "flow_chain": [
        {"name": str, "input_types": [str, ...],
         "output_types": [str, ...], "estimated_minutes": int},
        ...
    ],
    "all_document_types": [{"class_name": str, "description": str}, ...],
}
```

`input_document_types` names the root document types the pipeline accepts; `document_input_schema` and
`options_schema` are the resolved JSON Schemas a consumer validates a root input object and a run configuration
against before launching; `result_schema` is the shape of the delivered result; `flow_chain` is the declared phase
chain in 0.3.0 vocabulary. Each phase's `estimated_minutes` is the authored estimate declared on the phase
(`api/7-phases.md`, `Phase.estimated_minutes`); it is `0` when a phase declares none.

### Endpoint and example

The supported runtime exposes discovery as `GET /runtime/health`, `GET /pipelines`, and
`GET /pipelines/{name}` (the integration descriptor). Reading one pipeline's descriptor:

```text
GET /pipelines/review_app

200 →
{
    "deployment_name": "review_app",
    "input_document_types": [{"class_name": "ReviewRequestDocument", "description": "Root request document."},
                             {"class_name": "ReviewEvidenceDocument", "description": "Evidence supplied to the run."}],
    "document_input_schema": { ... },
    "options_schema": { ... },
    "result_schema": { ... },
    "flow_chain": [{"name": "AssessRiskPhase", "input_types": ["ReviewRequestDocument", "ReviewEvidenceDocument"],
                    "output_types": ["RiskAssessmentDocument"], "estimated_minutes": 3}],
    "all_document_types": [ ... ]
}
```

An unknown pipeline name is reported as not found; the error body names what was missing:

```text
GET /pipelines/unknown_app

(not found) →
{"error": "no such deployed pipeline: unknown_app"}
```

### Constraints

- The descriptor is self-describing and versioned: a consumer reads the accepted root document types, the
  document-input schema, the run-configuration schema, and the result schema from it, not from the pipeline's source
  (`runtime-api/1-overview-and-state.md § The externally readable record is versioned`).
- Discovery names what a consumer needs to launch; it does not expose framework internals or the topology beneath
  the surface.

## Launching a run

A consumer launches a run by naming the target pipeline and supplying a run identity, the run's root input
documents, and its run configuration. Launch is the out-of-process counterpart to `Runner.run`
(`advanced-api/1-runners-and-clients.md`): the same compiled pipeline, the same root input bundle and run
configuration, started over a boundary instead of in-process.

### Reference

The launch request:

```text
launch_request = {
    "run_id": str,                       # the run identity; see below
    "documents": [document_input, ...],  # the root input objects, or a single URL string
    "options": {...},                    # the run configuration, valid against options_schema
    "labels": {str: str},                # optional immutable run correlation metadata; see below
}
```

`documents` is the list of root-input objects below. As a convenience a consumer may pass `documents` as a single
URL string, which the framework normalizes to one fetchable-reference input (`{"url": "<url>"}`).

The launch response a consumer parses (real wire keys; this is a control-plane block):

```text
launch_response = {
    "run_id": str,        # the run identity the run is addressed by on every surface
    "state": RunState,    # the run's observed state at acceptance, e.g. PENDING
    "created": bool,      # true when this launch created the run; false when it adopted an existing identical run
}
```

### Run identity is the correlation key

- The consumer supplies the run identity at launch, or the framework assigns one; either way it is the one identity
  that addresses the run across every surface (`runtime-api/1-overview-and-state.md § One run identity`).
- Launch is idempotent on run identity: relaunching the same identity with the same normalized inputs and
  configuration adopts the existing run rather than starting a second one under the same identity.
- Relaunching the same identity with different inputs or configuration is rejected as a conflict; the framework does
  not silently start a divergent run under an identity already in use. This is the out-of-process face of the
  identity reuse rule in `advanced-api/1-runners-and-clients.md § Run identity is framework-assigned and stable`.

### Correlation labels are immutable run metadata

A consumer may attach `labels` — a flat map of string keys to string values — to correlate the run back to its own
external dimensions (an entity, an experiment, a batch) without encoding them into the run identity. The framework
records and equality-filters them but never interprets them, and application code never reads them.

- `labels` is optional: omitting the field, or supplying an empty map, records the run with no labels — identical to
  a launch from before the field existed.
- Labels are recorded once at creation and are immutable: a relaunch keeps the run's original labels, and
  re-associating work under a different label is a different run.
- Labels are **not** part of the idempotency/conflict key. Relaunching the same identity with the same normalized
  inputs and configuration but different labels adopts the existing run and keeps its recorded labels; it is not a
  conflict, since the conflict rule above turns only on inputs and configuration.
- Run correlation labels are distinct from the per-document provenance labels `derived_from`/`triggered_by` carried
  on a `document_input` below: those describe one root document's lineage, while correlation labels describe the run.
- Labels are launch metadata, not run configuration: run-varying values the application reads enter through
  `options` (`5-configuration.md`), never as labels.
- Labels are surfaced on the run record and the live lifecycle stream and are filterable by exact key/value equality
  only — never aggregation, grouping, or a query language (`runtime-api/4-run-record-read-model.md`,
  `runtime-api/3-observation-and-notifications.md`, `4-limits-and-non-promises.md § Out of scope entirely`).

### Root inputs enter as raw external material

A consumer cannot construct a typed root `Document`; it cannot import the document definitions. Instead it supplies
each root input as raw external material that the runtime resolves into a typed root document at the run boundary
(`api/2-documents.md § Root documents are created at the run boundary`).

### Reference

One root-input object:

```text
document_input = {
    "content": str | object | None,  # inline content; mutually exclusive with "url" (encoding below)
    "url": str | None,               # fetchable reference (https:// or gs://); null when "content" is set
    "class_name": str,               # target document type; required when the pipeline accepts >1 root type
    "name": str | None,              # document name; auto-derived from a URL path when omitted
    "description": str | None,       # optional read-model label
    "summary": str | None,           # optional read-model label
    "derived_from": [str, ...],      # opaque content-provenance labels; optional
    "triggered_by": [str, ...],      # opaque causal-provenance labels; optional
    "attachments": [attachment_input, ...],   # optional
}

attachment_input = {
    "content": str | None,   # inline bytes, base64-encoded; mutually exclusive with "url"
    "url": str | None,       # fetchable reference; null when "content" is set
    "name": str,             # required for inline content
    "media_type": str | None,  # optional; the runtime derives it from the bytes when omitted
    "description": str | None,
}
```

Exactly one of `content` and `url` is set on each object; supplying both, or neither, is rejected. `class_name` is
the only required field; `name`, `description`, `summary`, the provenance-label lists, and `attachments` are
optional. Companion byte material travels as `attachments` on the document that owns it.

Inline `content` carries the document's material in the shape its target `class_name` decodes:

- a **`Document[str]`** target takes a plain string;
- a **`Document[FrozenBaseModel]`** target takes a JSON object, validated against the pipeline's
  `document_input_schema` for that root type;
- a **`Document[bytes]`** target takes a base64-encoded string.

The runtime decodes `content` according to the target `class_name`; the consumer does not signal the encoding
separately.

### Constraints

- Each root input carries either inline content or a fetchable reference, never both and never neither. There is no
  by-identity input over this boundary: a consumer cannot pass a bare prior-document identity as a root input, since
  the input schema accepts only `content` or `url`. Re-submitting identical content reproduces the same content
  identity (`content_sha256`, `runtime-api/5-documents-and-logs.md`); it reproduces the same document identity
  (`document_sha256`) only when every identity-bearing field — name, description, provenance labels, and
  attachments — matches as well, so content re-submission is not a reliable way to re-enter a specific prior document
  for citation or provenance resolution. By-identity rehydration of a stored document, which does preserve its
  identity, is an in-process capability only (`advanced-api/1-runners-and-clients.md § Stored documents enter a run
  by identity`).
- Root inputs supply documents only; run-varying scalar values reach the application through run configuration, not
  as documents (`5-configuration.md`, `api/8-pipelines.md § RunConfig`).
- `class_name` is required whenever the pipeline accepts more than one root type; an ambiguous input is rejected
  rather than guessed.
- The runtime resolves raw inputs into typed root documents with framework-owned identity and root provenance; the
  consumer does not mint identity, and `derived_from`/`triggered_by` are recorded as opaque provenance labels, not
  fetch sources. The consumer never constructs a typed `Document[T]` over this boundary; it supplies raw material in
  the encoding the target `class_name` decodes (plain string, JSON object, or base64), and the runtime builds the
  typed document.
- A run that cannot validate its declared shape fails before any task executes; the framework does not start a run
  it has already proved cannot complete (`3-guarantees.md § Pre-run validation`).
- Accepted URL schemes, fetch size bounds, and content decoding are mechanism below this contract.

### Endpoint and example

The supported runtime exposes launch as `POST /pipelines/{name}/runs`. A launch with one inline root document and
one fetched root document:

```text
POST /pipelines/review_app/runs
{
    "run_id": "review-2026-06-14",
    "documents": [
        {"content": "Acme Corp, vendor risk review", "name": "request.md", "class_name": "ReviewRequestDocument"},
        {"url": "https://example.com/acme-filing.pdf", "class_name": "ReviewEvidenceDocument"}
    ],
    "options": {"assessment_model": "strong-model"}
}

(accepted) →
{"run_id": "review-2026-06-14", "state": "PENDING", "created": true}
```

Relaunching the same identity with the same normalized inputs adopts the existing run (`"created": false`);
relaunching it with different inputs or configuration is rejected as a conflict, and the error body names the
collision:

```text
(conflict) →
{"error": "run_id review-2026-06-14 already exists with different inputs"}
```

The same `POST /pipelines/{name}/runs` is used whether the runtime is the local full stack or a remote one;
only the base address differs (`5-configuration.md`).

### Launch confirmation is optional and bounded

A consumer may launch and return immediately, or block until the run is confirmed started or has failed during
startup, within a bounded window. The confirmed outcomes a consumer can rely on are: confirmed running; not picked
up within the window; failed at startup; crashed at startup; cancelled during startup; or the executor was
unavailable. The window length, poll cadence, and the mapping of these outcomes onto transport responses are
mechanism.

## Halting a run in flight

A consumer can halt a run by its identity, from outside the process that runs it, without killing that process and
without corrupting what was already recorded (`3-guarantees.md § Governable in-flight runs`). The operator-command
face of this is `ai-pipeline cancel` (`tools/8-cancel.md`).

### Endpoint and example

The supported runtime exposes halt as `POST /runs/{run_id}/cancel`. The response is the run's recorded terminal
state:

```text
POST /runs/review-2026-06-14/cancel

200 →
{"run_id": "review-2026-06-14", "state": "CANCELLED", "timestamp": "2026-06-14T12:31:07Z"}
```

### Constraints

- A halted run ends as a recorded `CANCELLED` terminal state, not as a crash; what it produced before the halt stays
  intact and readable.
- Halt is prompt and clean, not instantaneous: work already in flight when the signal arrives may finish and be
  recorded before the run stops (`4-limits-and-non-promises.md § Instantaneous cancellation`). Halt does not roll
  back work already completed and recorded.
- Halting an already-terminal run is a successful no-op that reports the run's existing terminal state.

## Reconciling a crashed or interrupted run

A consumer can ask for a run's authoritative state, resolved against the durable record rather than inferred from the
last live signal or from elapsed time (`3-guarantees.md § Reconcile to recorded truth`). Reconciliation is how a
`CRASHED` run is distinguished from a quiet `RUNNING` one and how a dropped or stale final signal is prevented from
becoming a confident wrong answer about whether a run finished. The operator-command face is `ai-pipeline recover`
(`tools/9-recover.md`).

### Reference

The reconciled run-state response:

```text
run_state_response = {
    "run_id": str,
    "state": RunState,                # PENDING | RUNNING | COMPLETED | FAILED | CANCELLED | CRASHED
    "timestamp": datetime,
    "result": result_payload | None,  # present for a completed run, else null
    "actual_cost": float | None,      # total recorded cost when known
    "error": str | None,              # terminal failure reason when the run failed
}

result_payload = {
    "success": bool,
    "error": str | None,
    # ... plus the pipeline's declared result fields
}
```

A completed run's `result` is always a `result_payload`: a thin framework envelope of `success` and an optional
`error`, carrying alongside it the fields declared by the pipeline's result shape. `result_schema` in the launch
descriptor describes **those declared result fields** — the application's delivered output bundle — not the
`success`/`error` envelope around them, which is framework-owned and the same for every pipeline. A consumer
validates the application's outputs against `result_schema` and reads `success`/`error` as the fixed envelope keys.
`state` is the shared `RunState` (`runtime-api/1-overview-and-state.md § The observed run-state is one shared type`).

### Endpoint and example

The supported runtime exposes reconciliation as `GET /runs/{run_id}/state`. A run whose worker died returns a
reconciled `CRASHED` rather than a stale `RUNNING`:

```text
GET /runs/review-2026-06-14/state

200 →
{
    "run_id": "review-2026-06-14",
    "state": "CRASHED",
    "timestamp": "2026-06-14T12:40:11Z",
    "result": null,
    "actual_cost": 1.82,
    "error": "process crashed; no terminal transition recorded"
}
```

A completed run returns its delivered result in the same shape:

```text
GET /runs/review-2026-06-14/state

200 →
{"run_id": "review-2026-06-14", "state": "COMPLETED", "timestamp": "...",
 "result": {"success": true, "error": null, "assessment": { ... }}, "actual_cost": 2.41, "error": null}
```

### Constraints

- Reconciliation reads the orchestrator's live state where available and otherwise the durable record; the
  record-derived account is authoritative over a stale live observation.
- A still-running record is reconciled to a terminal state only on explicit orchestrator-terminal evidence (the run
  failed, crashed, or was cancelled). A record the orchestrator reports completed while its durable units are still
  running is flagged for operator intervention, never silently marked complete.
- Reconciliation establishes the true recorded state; it does not fabricate state the run never recorded, and it does
  not resume the run. Continuing a reconciled run from where it stopped is the resume capability of `ai-pipeline run`
  and `Runner.resume` (`advanced-api/1-runners-and-clients.md`), not reconciliation.
- Wall-clock reconciliation — concluding a run died purely from elapsed time without orchestrator evidence — is an
  explicit, operator-accepted fallback named as nondeterministic, not the default behavior.
- The reconciled answer is a full `RunState` reading and, when terminal, the delivered result or the failure reason
  and the run's recorded cost (`runtime-api/4-run-record-read-model.md`).

## Out of scope

- **Deployment-specific preflight checks.** A specific deployment may offer its own input preflight — for example,
  validating access to an external source before launch. Those are deployment features, not framework runtime
  semantics, and are not part of this contract.
- **The trust boundary.** Who may launch, halt, or reconcile a run, and how the control plane is exposed to outside
  callers, stay at the platform edge (`4-limits-and-non-promises.md § Out of scope entirely`).
