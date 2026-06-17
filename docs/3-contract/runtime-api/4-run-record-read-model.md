# Reading a run's record from outside

This file defines the externally readable read models for a run's record: the runs themselves, their observed state,
their execution tree, the assembled reconstruction, the child pipelines nested inside a run, and the recorded cost.
These read models are the out-of-process projection of the in-process read seam in `advanced-api/2-database.md`: the
same recorded facts, served to a reader that cannot import the framework's definitions, with the server-side
pagination, filtering, and projection that in-process file explicitly defers to here. Documents and logs are read
through `runtime-api/5-documents-and-logs.md`.

These read models are versioned and self-describing: a reader that meets a shape it does not understand fails loudly
with a version mismatch rather than mis-decoding (`runtime-api/1-overview-and-state.md § The externally readable
record is versioned`). The shapes below name the fields that cross the boundary; deeper internal structure is not
promised to outside readers. Each field carries its 0.3.0 name; where the wire spelling differs (for example `flow`
for phase, `deployment` for a pipeline run), the real wire field is annotated alongside it so an integrator can find
it on the boundary. The wire token `deployment` denotes a pipeline *run/execution* (an execution attempt of a
deployed pipeline), distinct from a published *deployment* (`deployment_name`, the result of `ai-pipeline deploy`).

## The first-class run reads

A consumer reads a run's authoritative state first-class — its summary and observed state — and then descends into
its execution tree, exactly as the in-process read seam does, but over the boundary.

### Capabilities

- **List runs.** Enumerate recorded runs, with server-side pagination, a status filter, and a label-equality filter.
  Only top-level runs are listed; a child pipeline appears as a nested subtree within its parent run, not as a
  separate top-level run.
- **Get one run summary.** By run identity: the run's observed state, the pipeline name, start and end times, the
  terminal failure reason when it failed, and its total recorded cost.
- **Get the delivered outputs.** The delivered result of a completed run (`result_payload`,
  `runtime-api/2-run-control.md`), read back from the record. This is where a consumer reads a result too large to
  have been delivered as a lifecycle event (`runtime-api/3-observation-and-notifications.md`).
- **Get the execution tree.** The recorded units of work for a run, each with status, timing, cost, and the
  documents it consumed and produced. The tree is readable whole, or scoped to one child pipeline's subtree so a
  large nested run can be loaded lazily.
- **Get the assembled reconstruction.** The execution tree assembled against the run's declared phase plan, so a
  consumer sees the planned shape and actual progress together; while a run still executes, declared-but-not-yet-
  started units appear as pending.

### Reference

The run summary and one unit of the execution tree:

```text
run_summary = {
    "run_id": str,
    "execution_id": str,          # the recorded execution attempt; disambiguates a reused run_id
    "pipeline_name": str,         # wire field: flow_name
    "state": RunState,            # wire field: status; the run's observed RunState (see Constraints)
    "start_time": str | None,
    "end_time": str | None,
    "total_cost_usd": float,      # wire field: total_cost
    "total_tokens": int,
    "labels": {str: str},         # the run's correlation labels (empty when none supplied at launch)
}

run_unit = {                      # one span: a pipeline run, phase, task, judgment, or finer unit
    "span_id": str,               # the unit identity
    "parent_span_id": str | None,
    "kind": str,                  # wire field: span_type
                                  # 0.3.0: pipeline | phase | task | judgment | (finer units beneath)
                                  # wire:   deployment | flow | task | prompt_execution | llm_round | tool_call
    "name": str,
    "status": str,                # per-unit status: running | completed | failed | cached | skipped
                                  # (a unit-level status — cached is a cache hit, skipped is a gated/bypassed unit;
                                  #  this is not the run-level RunState)
    "start_time": str | None,
    "end_time": str | None,
    "duration_ms": int | None,
    "cost_usd": float,            # wire field: cost
    "tokens_input": int,
    "tokens_output": int,
    "tokens_cached": int,
    "tokens_reasoning": int,
    "error_message": str,
    "input_document_ids": [str, ...],   # wire field: input_doc_sha256s
    "output_document_ids": [str, ...],  # wire field: output_doc_sha256s
}
```

The assembled reconstruction nests units under their phase and the run's declared plan:

```text
run_reconstruction = {
    "run": run_summary,
    "flow_plan": [{"name": str, "flow_class": str, "step": int,
                   "estimated_minutes": float, "expected_tasks": [...]}, ...],
    "flows": [                    # one entry per phase (wire token: "flow")
        {"flow_name": str, "flow_class": str, "step": int, "status": str,
         "duration_ms": int | None, "tasks": [task_node, ...],
         "output_documents": [document_ref, ...]},
        ...
    ],
    "result": result_payload | None,
    "error": str | None,
}
```

A child pipeline nested in a run is listed as its own scoped subtree:

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

### Endpoint and example

The supported runtime exposes the run reads as `GET /runs` (list, paginated, status-filtered, and label-filtered),
`GET /runs/{run_id}` (summary), `GET /runs/{run_id}/outputs` (delivered result), and `GET /runs/{run_id}/tree`
(execution tree, optionally scoped to a child subtree). Reading one summary, with its wire field names on the
boundary:

```text
GET /runs/review-2026-06-14

200 →
{
    "run_id": "review-2026-06-14",
    "execution_id": "exec-9KQ2",
    "flow_name": "review_app",          # 0.3.0 name: pipeline_name
    "status": "completed",              # 0.3.0 name: state; the recorded RunState, lowercase on the wire
    "start_time": "2026-06-14T12:30:00Z",
    "end_time": "2026-06-14T12:34:01Z",
    "total_cost": 2.41,                 # 0.3.0 name: total_cost_usd
    "total_tokens": 51200,
    "labels": {}                        # the run's correlation labels; none were set at launch
}
```

Listing runs filtered by correlation labels repeats the `label` query parameter, one `key:value` per required
label; a run matches only when it carries every requested label (exact equality):

```text
GET /runs?label=entity:acme&label=experiment:alpha
```

### Constraints

- A consumer reads a run's observed state, terminal reason, and delivered outputs from the run summary and outputs
  reads, not by reconstructing them from a walk of the execution tree.
- `state` on the summary is the one shared `RunState` (`runtime-api/1-overview-and-state.md § The observed run-state
  is one shared type`). A raw summary or list read returns the **recorded** state — one of `PENDING`, `RUNNING`,
  `COMPLETED`, `FAILED`, `CANCELLED` — and the **reconcile read** (`runtime-api/2-run-control.md`) is the only
  producer of `CRASHED`, so an unreconciled crashed run reads `RUNNING` until reconciliation resolves it. This is
  endpoint authoritativeness — *which call you made determines how authoritative the state is* — not a separate
  run-state vocabulary. `cached` and `skipped` are unit-level statuses on `run_unit.status`, never run-level
  `RunState`.
- The **list-runs status filter** accepts `RunState` values. A consumer filtering the list filters by run state, not
  by a unit-level token; `cached` and `skipped` are unit facts and are not run-level filter values.
- The **list-runs label filter** matches runs by exact label key/value equality, alongside the status filter. It is
  an equality filter only — no aggregation, grouping, or query language over labels — keeping the run record a
  record and not an analytics warehouse (`4-limits-and-non-promises.md § Out of scope entirely`). A run's `labels`
  are immutable correlation metadata set at launch (`runtime-api/2-run-control.md § Correlation labels are immutable
  run metadata`); the framework records and filters them but does not interpret them.
- Listing and filtering happen server-side for an external reader; the consumer does not load every run to filter in
  its own memory (this is the projection `advanced-api/2-database.md § The read seam exposes recorded runs` defers to
  the runtime API).
- A reused run identity is disambiguated by `execution_id`, the recorded attempt reference point, so a read resolves
  the intended execution attempt (`runtime-api/1-overview-and-state.md § One run identity`).
- The execution tree commits to the per-unit fields named here; the full internal structure of a unit is a framework
  internal and may not be relied on as contract.

## Recorded cost and metrics

A run records what it spent, and that recorded cost is part of what this surface provides
(`3-guarantees.md § Cost recording and attribution`). Cost is read from the record, never derived by the consumer
and never read from authoring result objects — a `PromptResult` carries no cost (`api/4-prompt-contracts.md`).

Cost is exposed at every level of the record: the run's total on the `run_summary`, each unit's cost on its
`run_unit`, and each nested child pipeline's cost on its `child_pipeline_summary` (which also contributes to the
parent run's total — `runtime-api/6-child-pipelines.md`). Above those, the run aggregate rolls the record up for a
consumer that wants a run's spending and shape at a glance.

### Reference

```text
run_aggregate = {
    "run_id": str,
    "root_execution_id": str,             # wire field: root_deployment_id; the run-root execution identity
    "deployment_name": str,               # the pipeline name
    "service": str,                       # a coarse run-class label, not a tenancy key (see Constraints)
    "labels": {str: str},                 # the run's correlation labels (see runtime-api/2)
    "status": str,
    "total_cost_usd": float,
    "total_duration_ms": int,
    "total_llm_calls": int,               # model calls (llm_round spans), not judgments; a repair
                                          # loop makes one judgment incur several model calls
    "failed_llm_calls": int,
    "dominant_model": str | None,
    "dominant_llm_error_type": str | None,
    "failing_step_name": str | None,      # the failing phase
    "failing_span_id": str | None,        # the failing unit
    "failing_exception_class": str | None,
    "finish_reason": str | None,
    "per_flow": [per_flow_summary, ...],  # one entry per phase
}

per_flow_summary = {                      # one phase (wire token: "flow")
    "step": int,
    "name": str,
    "flow_class": str,
    "status": str,
    "duration_ms": int,
    "cost_usd": float,
    "llm_calls": int,                     # model calls (llm_round spans) in this phase, not judgments
    "failed_llm_calls": int,
}
```

Token totals — input, output, cache-read, and reasoning tokens — accompany cost on a unit wherever the framework
recorded them (`advanced-api/2-database.md § The read seam exposes recorded runs`).

### Endpoint and example

The supported runtime exposes the aggregate as `GET /runs/{run_id}/aggregate`, the rolled-up cost-and-shape view a
consumer reads to see a run's spending at a glance and per phase:

```text
GET /runs/review-2026-06-14/aggregate

200 →
{
    "run_id": "review-2026-06-14",
    "root_deployment_id": "review-2026-06-14",
    "deployment_name": "review_app",
    "status": "completed",
    "total_cost_usd": 2.41,
    "total_duration_ms": 241000,
    "total_llm_calls": 18,
    "failed_llm_calls": 0,
    "dominant_model": "strong-model",
    "finish_reason": "completed",
    "per_flow": [
        {"step": 1, "name": "AssessRiskPhase", "flow_class": "AssessRiskPhase", "status": "completed",
         "duration_ms": 241000, "cost_usd": 2.41, "llm_calls": 18, "failed_llm_calls": 0}
    ]
}
```

`total_llm_calls` and `per_flow[].llm_calls` count model calls, not judgments — a repair loop makes one judgment
incur several model calls.

### Constraints

- Cost is aggregated per run, per phase, per task, and per judgment; a consumer reads the attribution it needs from
  the level it asks for, rather than recomputing it (`3-guarantees.md § Cost recording and attribution`).
- Cost attribution extends down to the individual judgment and across phases and tasks. It does not extend to a
  per-requester or per-tenant key: that is the multi-tenant trust boundary, which is out of scope
  (`4-limits-and-non-promises.md § Out of scope entirely`). A coarse run-class or service label the framework may
  record alongside an aggregate is a descriptive tag, not a tenancy key.
- A consumer reads recorded cost and status from the record; it does not infer either from timing.
