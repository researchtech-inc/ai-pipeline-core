# Child pipelines run the same way local or remote

This file defines the single child-pipeline contract that behaves identically whether the child executes in the
parent's process or as a separately-deployed run. From the end user's point of view there is one child-pipeline
boundary; whether it runs local or remote is a framework-owned placement decision, not a difference the author or the
operator codes against.

## One boundary, framework-owned placement

A parent schedules a child pipeline through one boundary: it supplies the concrete child `Pipeline` class, a child
run configuration, and a child input bundle, and receives the child's declared output bundle (`api/7-phases.md §
f.child_pipeline`, `api/9-integrations.md § Child pipelines are scheduled by phase builder`). The author always
writes that one boundary, addressing the child by its imported `Pipeline` class — there is no second authored form
and no remote-target string. The parent receives the same output-bundle shape, and the same failure semantics,
whether the child runs:

- **local** — executed in the parent's process; or
- **remote** — executed as a separately-deployed run.

Which of the two happens is selected by configuration — the execution backend — not by the authored shape. For
remote execution the framework resolves the declared child `Pipeline` class to its deployed counterpart; the author
does not write that resolution. This is a direct application of location independence: a run behaves identically
whether it executes in one process or across many, and the framework owns where and when each unit runs
(`3-guarantees.md § Location independence`). When the configured backend cannot support cross-process execution, the
framework runs the child in-process and the result is identical.

### Constraints

- The author-facing child-pipeline boundary is the same for both placements; the author always passes the concrete
  child `Pipeline` class, never a deployed-pipeline reference or remote-target string, and does not encode placement
  into the declared shape.
- The framework resolves the declared child `Pipeline` class to a local in-process execution or a deployed remote
  run; that resolution is framework-owned and configuration-driven, not an authored decision.
- Invoking a deployed child imperatively from inside task code (for example, polling a remote run with a fallback)
  is a legacy consumer pattern and a migration target, not a supported authored shape; the supported shape is the
  one declarative `f.child_pipeline` boundary. `api/7-phases.md` and `api/9-integrations.md` describe that boundary
  and point here for placement-independent execution.

## The child has its own record, correlated to the parent

A child pipeline has its own input bundle, run configuration, output bundle, and run record
(`api/9-integrations.md`). Whether local or remote, the child's run is correlated into the parent run so a consumer
reading from outside sees one connected execution.

### Constraints

- The child's run is linked to the parent through the parent run's root identity, carried on every child record and
  every child lifecycle event (`runtime-api/3-observation-and-notifications.md`).
- A child run identity is framework-derived from the parent's identity and the child's inputs, or framework-assigned,
  and is deterministic for the same inputs so a repeated schedule reuses the same child run rather than starting a
  divergent one (the out-of-process face of the idempotent-launch rule in `runtime-api/2-run-control.md`).
- In the run-record read models, a child pipeline appears as a nested subtree of the parent run rather than as a
  separate top-level run, and is loadable as its own scoped subtree (`runtime-api/4-run-record-read-model.md`).
- An external reader addresses both the child record and the parent record through these correlated identities and
  never invents its own correlation scheme (`runtime-api/1-overview-and-state.md § One run identity`).
- A child run inherits the parent run's correlation labels, recorded on the child's own run record identically
  whether the child runs local or remote, and surfaced on the child summary and the child's lifecycle events.
  Top-level run listing returns only parent runs (`runtime-api/4-run-record-read-model.md`); the inherited labels
  are read on the child where the child is read — in the parent's tree or as the child's own scoped subtree
  (`runtime-api/2-run-control.md § Correlation labels are immutable run metadata`).

### Reference

A child pipeline surfaces in the parent's run record as a `child_pipeline_summary` — the same shape the read models
define (`runtime-api/4-run-record-read-model.md`), carrying the child's own run identity, the parent unit that
scheduled it, and the child's status and cost:

```text
child_pipeline_summary = {
    "execution_id": str,          # wire field: deployment_id; the child run's execution attempt
    "run_id": str,                # the child run identity
    "pipeline_name": str,         # wire field: flow_name
    "parent_span_id": str | None, # the unit in the parent that scheduled the child
    "status": str,
    "start_time": str | None,
    "end_time": str | None,
    "cost_usd": float,            # wire field: cost_usd
    "labels": {str: str},         # the child run's correlation labels, inherited from the parent
}
```

The same shape is returned whether the child ran in the parent's process or as a separately-deployed run; placement
is not a field on it.

### Example

A parent run schedules a vendor-review child. Reading the parent's tree shows the child as a nested subtree, and the
child is loadable on its own by its run identity through the same read endpoints — identically for a local and a
remote child:

```text
GET /runs/review-2026-06-14/tree

200 →
{
    "run_id": "review-2026-06-14",
    "units": [ ... parent units ... ],
    "children": [
        {"deployment_id": "exec-vc7", "run_id": "review-2026-06-14:vendor-review",
         "flow_name": "vendor_review_app", "parent_span_id": "9KQ2-vendor-phase",
         "status": "completed", "cost_usd": 0.91}
    ]
}

GET /runs/review-2026-06-14:vendor-review/tree     # the child loaded as its own scoped subtree

200 →
{"run_id": "review-2026-06-14:vendor-review", "root_deployment_id": "review-2026-06-14", "units": [ ... ]}
```

The child's `run_id` is framework-derived from the parent's identity and the child's inputs, so a repeated schedule
reuses the same child run; `root_deployment_id` on the child record and on every child lifecycle event correlates it
back to the parent. The parent receives the child's declared output bundle in the same shape, and the child appears
in the read model the same way, regardless of whether placement was local or remote.

## Cost and failures cross the boundary identically

### Constraints

- A child pipeline's recorded cost contributes to the parent run's total and is also attributable to the child run
  on its own (`runtime-api/4-run-record-read-model.md § Recorded cost and metrics`).
- A child's terminal failure propagates to the parent phase boundary identically whether the child ran local or
  remote; a retryable child failure is retried under the framework's policy
  (`api/9-integrations.md`, `3-guarantees.md § Failure handling and retry`).
- A parent that expects a child to fail as part of normal business state models that outcome as a typed document,
  not as an exception to catch (`api/9-integrations.md`); this is identical for both placements.
