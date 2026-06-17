# Observing a run and being notified

This file defines how an external system observes a run while it executes and catches up on what it missed. A run is
observable live, not only after it ends, but the live signal is best-effort and the durable record is the
authoritative account (`3-guarantees.md § Live observation with authoritative recovery`).

## Two ways to observe, both first-class

A consumer learns that a run's state changed in one of two ways, and both are first-class: by **subscribing** to the
published lifecycle stream, or by **polling** the catch-up read that reconstructs the lifecycle from the durable
record. Neither is the privileged path. A consumer that can subscribe gets the lowest-latency signal; a consumer
that cannot, or that prefers not to hold a subscription, drives the entire observation by polling the catch-up read
on a cursor — it loses nothing, because every transition is reconstructable from the record. Both reconcile against
the record, which is authoritative.

There are **no webhooks** and no callback URLs in this surface: the framework does not call back into a
consumer-supplied endpoint, and it does not push outputs to a sink. A consumer reads a run's result back from the
record (`runtime-api/4-run-record-read-model.md`).

## The lifecycle event is a CloudEvents 1.0 envelope

Every lifecycle event is a CloudEvents 1.0 structured envelope. The `specversion` and `datacontenttype` are pinned
literals: this is a versioned, consumer-facing interop shape an integrator parses, not a wire detail that varies.

### Reference

```text
lifecycle_event = {
    "id": str,                              # unique per published event; "<type>:<span_id>" on the catch-up variant
    "source": str,                          # "ai-<service>-worker" live; "lifecycle-catchup" on catch-up
    "type": str,                            # the transition token (see below)
    "specversion": "1.0",                   # pinned
    "time": str,                            # RFC 3339 timestamp
    "subject": str,                         # the run identity
    "datacontenttype": "application/json",  # pinned
    "data": {
        "run_id": str,                      # the run identity
        "seq": int,                         # per-publisher sequence number (see Constraints)
        "root_deployment_id": str,          # the enclosing run-root identity, on every event
        "span_id": str,                     # the unit identity the transition is about
        "parent_span_id": str,              # the scheduling unit
        "label_keys": [str, ...],           # the producing run's correlation labels (keys),
        "label_values": [str, ...],         #   paired by position with label_keys (empty when unlabeled)
        # transition-specific fields below, e.g. status, flow_name, step, total_steps,
        # input_document_sha256s, output document refs, error_message, duration_ms;
        # the run.completed event carries the delivered result_payload (2-run-control.md);
        # the catch-up variant additionally carries "cursor" and "event_key".
    },
}
```

The published stream also sets message attributes a subscriber filters and routes on without decoding the body:
`service_type`, `event_type`, `run_id`, and `root_deployment_id`, plus one `label.<key>` attribute per correlation
label on the producing run (value: the label's value). A subscriber routes on a correlation dimension — for example
`label.entity` — at the bus level without parsing the event body. The labels are the producing run's immutable
correlation labels (`runtime-api/2-run-control.md § Correlation labels are immutable run metadata`); like every
event field they are denormalized for routing, and the durable record remains authoritative.

The `type` token names the transition. In the framework's 0.3.0 vocabulary these are run, phase, and task
transitions; on the wire the phase transitions carry the `flow.*` token:

- **Run lifecycle.** `run.started`, `run.completed`, `run.failed`, and a periodic `run.heartbeat` liveness signal.
- **Phase lifecycle.** `flow.started`, `flow.completed`, `flow.failed`, and `flow.skipped`. `flow.skipped` is a
  coarse liveness signal — the phase did not actively execute — covering a phase a gate routed around, a phase served
  entirely from cache, and a phase bypassed on resume. A consumer that needs to tell those apart reads the unit's
  recorded `status` in the read model, which distinguishes a cache hit (`cached`) from a skip (`skipped`)
  (`runtime-api/4-run-record-read-model.md`); the live event token is not where that distinction lives.
- **Task lifecycle.** `task.started`, `task.completed`, and `task.failed`.

### Example

One `task.completed` envelope as a subscriber receives it:

```text
{
    "id": "task.completed:9KQ2-assess-risk",
    "source": "ai-review-worker",
    "type": "task.completed",
    "specversion": "1.0",
    "time": "2026-06-14T12:30:55Z",
    "subject": "review-2026-06-14",
    "datacontenttype": "application/json",
    "data": {
        "run_id": "review-2026-06-14",
        "seq": 41,
        "root_deployment_id": "review-2026-06-14",
        "span_id": "9KQ2-assess-risk",
        "parent_span_id": "9KQ2-assess-phase",
        "status": "completed",
        "flow_name": "AssessRiskPhase",
        "output_document_sha256s": ["7QF3K2ABCD"],
        "duration_ms": 4120
    }
}
```

### Constraints

- `specversion` is `"1.0"` and `datacontenttype` is `"application/json"`; a consumer relies on both as pinned.
- Every event carries `root_deployment_id`, the enclosing run-root identity, so a consumer correlates a child
  pipeline's events into the parent run that scheduled it rather than inventing its own scheme
  (`runtime-api/1-overview-and-state.md § One run identity`, `runtime-api/6-child-pipelines.md`).
- `seq` increments per publishing process, not per run; across the workers of a child-bearing run, sequence numbers
  are independent. The cross-process ordering and resume key is the catch-up `cursor`, not `seq`.
- Every observable transition is recorded in the durable record independent of whether its live delivery succeeded,
  so a degraded or absent live channel never costs observability (`3-guarantees.md § Live observation`).
- The `run.heartbeat` event is a liveness signal only: not a state transition, not reconstructable from the record,
  and never the basis for inferring a terminal state — that is reconciliation's job (`runtime-api/2-run-control.md`).
- A `run.completed` event whose delivered result exceeds the publish-size limit is not published at all; the run is
  still recorded as completed in the durable record, so a consumer reads an over-size result from the record
  (`runtime-api/4-run-record-read-model.md`) and does not depend on the completed event to carry it. The framework
  never silently truncates a result to fit it into an event.

## Catch-up is reconstructed from the record, at least once

A watcher connects late, drops, and reconnects. A consumer recovers exactly the stretch of lifecycle it missed by
reading the events the framework reconstructs from the durable record, addressed by an opaque cursor. This is the
mechanism by which the best-effort live channel never costs observability.

### Constraints

- Lifecycle events are reconstructable from the durable record for catch-up; a consumer that missed a stretch reads
  it back rather than losing it (`3-guarantees.md § Live observation`).
- A consumer resumes catch-up from an opaque, monotonic cursor and receives the events after it. The cursor is
  opaque: a consumer carries it forward and does not parse structure out of it.
- Delivery is at-least-once, not exactly-once (`4-limits-and-non-promises.md § Exactly-once live delivery`). The
  catch-up read replays a small overlap window around the cursor so no event is lost across a reconnect, which means
  a consumer can see an event more than once.
- A consumer deduplicates redundant deliveries by the pair of the unit identity and the transition kind — the
  `(span_id, type)` pair, derived from `data.span_id` and `type` on a live event and exposed directly as the
  catch-up event's `id` (and `event_key`) — and merges the live stream with the catch-up read against the record. A
  live event's top-level `id` is unique per published delivery and is not the dedup key; the `(span_id, type)` pair
  is. The record, not the last observed signal, is the authoritative account.
- The catch-up read reports whether the run has reached a terminal run-level transition, so a consumer can tell a
  finished run's stream from one that is still open.
- The heartbeat is live-only and is not part of the reconstructable catch-up stream.

### Endpoint and example

The supported runtime exposes the catch-up read as `GET /runs/{run_id}/events?after_cursor=<cursor>`. A consumer
that drives observation entirely by polling calls it in a loop, carrying the returned cursor forward until the
stream reports terminal:

```text
GET /runs/review-2026-06-14/events            # first call: omit after_cursor for the whole stream so far

200 →
{
    "events": [ { "id": "run.started:review-2026-06-14", "type": "run.started", "specversion": "1.0", ... },
                { "id": "task.completed:9KQ2-assess-risk", "type": "task.completed", ... } ],
    "cursor": "opaque-cursor-token-2",
    "is_complete": false
}

GET /runs/review-2026-06-14/events?after_cursor=opaque-cursor-token-2

200 →
{ "events": [ { "id": "run.completed:review-2026-06-14", "type": "run.completed", ... } ],
  "cursor": "opaque-cursor-token-3", "is_complete": true }
```

The overlap window means an event may appear in two successive reads; a consumer deduplicates by the `(span_id, type)`
pair — the catch-up event's `id` — and stops when `is_complete` is true. A subscriber and a poller of the same run see the
same transitions and merge them against the record without duplication.

## Observing documents as they become durable

Beyond execution transitions, a consumer can observe when each document in a run became durable and walk the run's
recent document events, filtered and bounded. This is the live face of the recorded document events in the read
models (`runtime-api/5-documents-and-logs.md`); the live document-event stream and the record-derived document
events carry the same identities so a consumer merges them without duplication. A document event carries the
producing run's identity; a consumer that wants the run's correlation labels resolves them through that run, not
from the document event itself.
