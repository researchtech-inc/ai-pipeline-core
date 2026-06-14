# `ai-pipeline inspect`

Examines a run's durable record: its status and delivered outputs, its spans, its unified logs, its recorded cost,
the documents it produced, and the provenance relationships between them — and assembles a self-contained markdown
trace bundle when one is wanted. It is the operator face of the read seam (`advanced-api/2-database.md`) and the
out-of-process read models (`runtime-api/4-run-record-read-model.md`, `runtime-api/5-documents-and-logs.md`). There
is no separate trace tool; the bundle workflow lives here. `6-tools-and-the-development-loop.md` lists the loop.

## Synopsis

```text
ai-pipeline inspect <run-id> [--spans] [--logs [--level <L>] [--category <C>]] [--cost]
                             [--provenance <document-id>] [--outputs] [--remote] [--json]
ai-pipeline inspect <run-id> --compare <other-run-id> [--bundle <dir>] [--remote] [--json]
ai-pipeline inspect <run-id> --bundle <dir> [--compare <other-run-id>] [--remote]
ai-pipeline inspect --list [--limit <n>] [--status <status>] [--remote] [--json]
```

## Options

- `<run-id>` — the run to read.
- `--spans` — the execution tree, each unit with status (incl. computed vs served-from-cache), timing, and cost.
- `--logs` — the run's unified diagnostic account, filterable by `--level` and `--category`.
- `--cost` — the recorded cost, aggregated per run, per phase, per task, and per judgment.
- `--provenance <document-id>` — walk forward and reverse provenance from a document to trace a conclusion to its
  evidence and find everything it affected.
- `--outputs` — the delivered output bundle of a completed run.
- `--compare <other-run-id>` — compare the recorded behavior (cost, tokens, output) of `<run-id>` against
  `<other-run-id>`. It is standalone — without `--bundle` it emits the structured comparison; with `--bundle` it
  also writes the readable markdown comparison bundle.
- `--bundle <dir>` — assemble a self-contained markdown trace bundle into `<dir>`; combined with `--compare` the
  bundle is an original-vs-replay comparison.
- `--list` — enumerate recorded runs, with `--limit` and a `--status` filter (`--status` accepts `RunState` values).
- `--remote` — read across the runtime boundary instead of the in-process store; same reads, same shapes.
- `--json` — emit the structured record for automated consumption.

## Output

A run's authoritative state read first-class — its summary, terminal status, and delivered outputs — and, below
that, the spans, the unified logs (the diagnostic account of every part that touched the run, correlated to it), the
recorded cost and status of each unit, and the forward and reverse provenance of any document — all consumable in
bounded portions rather than as one undivided dump. The structured form is the same record in machine-parseable
shape; the bundle form is human-and-agent-readable markdown.

## Examples

Read a run's status, outputs, and cost; works the same on a local run and a remote one:

```text
ai-pipeline inspect review-2026-06-14 --outputs --cost
ai-pipeline inspect review-2026-06-14 --outputs --cost --remote
```

Trace a challenged conclusion back to its evidence:

```text
ai-pipeline inspect review-2026-06-14 --provenance 7QF3K2ABCD --json
```

Read the unified logs for a run, filtered to errors:

```text
ai-pipeline inspect review-2026-06-14 --logs --level ERROR
```

Build a comparison bundle between a baseline run and a replayed candidate:

```text
ai-pipeline inspect review-2026-06-14 --bundle ./bundle --compare review-2026-06-14-cheap-model
```

## Guarantees

- A run's status and delivered outputs are read directly, not reconstructed from span walks; a challenged
  conclusion is traceable to its sources from the record alone, without rerunning the work.
- A run's logs are readable by run and by unit as one cross-subsystem account, and the record distinguishes a
  computed unit from one served from cache (`3-guarantees.md § Durable, readable logs`,
  `§ Preserved, traceable, replayable runs`).
- `ai-pipeline inspect` reads the durable record at any point, including while the run is still executing; the
  durable record is the authoritative account. Continuous live streaming for an external system is the runtime
  surface (`runtime-api/3-observation-and-notifications.md`), not this command.

## Does not guarantee

- That the record judges whether a conclusion is correct; it shows where the conclusion came from, and the judgment
  of correctness remains a review decision (`4-limits-and-non-promises.md`).
