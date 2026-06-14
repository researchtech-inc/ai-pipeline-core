# `ai-pipeline recover`

Reconciles a crashed or interrupted run to its recorded truth. It is the operator face of the reconcile capability
the runtime surface also exposes (`runtime-api/2-run-control.md`). `6-tools-and-the-development-loop.md` lists the
loop.

## Synopsis

```text
ai-pipeline recover <run-id> [--json]
```

## Options

- `<run-id>` — the run to reconcile.
- `--json` — emit the reconciled authoritative state.

## Output

The run's reconciled authoritative state, established from the record: a full `RunState` reading and, when terminal,
the delivered result or the failure reason and the run's recorded cost. The structured form carries that state for
automated consumption.

## Examples

Establish a crashed run's authoritative state before deciding whether to resume it:

```text
ai-pipeline recover review-2026-06-14 --json
```

## Guarantees

- The state is reconciled against the durable record, not inferred from the last observed signal or from elapsed
  time, so a crashed run and a quiet one are distinguishable and a dropped or stale final signal does not become a
  wrong answer about whether the run finished or how it ended (`3-guarantees.md § Reconcile to recorded truth`).

## Does not guarantee

- That it recovers work the run never recorded; it establishes the true state of what was recorded. Continuing a
  reconciled run from where it stopped is `ai-pipeline run`'s resume capability, not `ai-pipeline recover`.
