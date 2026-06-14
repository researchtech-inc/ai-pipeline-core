# `ai-pipeline run`

Loads an application's declared package and executes its pipeline — whole, as a contiguous phase range, or one unit
in isolation — and resumes interrupted runs. The application declares and exports its runnable units; `ai-pipeline
run` loads them and runs them, so the application needs no `__main__` or other entrypoint of its own. This is the
center of the loop; `6-tools-and-the-development-loop.md` lists the rest. It is the operator face of the runner in
`advanced-api/1-runners-and-clients.md` and shares its guarantees.

## Synopsis

```text
ai-pipeline run <package> --inputs <path|inline> --run-config <path|inline> [--run-id <id>]
                          [--cache reuse|refresh|bypass] [--remote] [--json]
ai-pipeline run <package> --resume --run-id <id> [--cache reuse|refresh|bypass] [--remote] [--json]
ai-pipeline run <package> --run-id <id> --start-after <Phase> --stop-after <Phase> [--cache ...] [--json]
ai-pipeline run <package> --task <TaskClass> --inputs <path|inline> [--run-id <id>] [--cache ...] [--json]
ai-pipeline run <package> --contract <ContractClass> --inputs <path|inline> --model <AIModelRef> [--run-id <id>] [--json]
```

## Options

- `<package>` — the application package to load and run.
- `--inputs <path|inline>` — the root input bundle for a full run, or the resolved inputs for a single unit.
- `--run-config <path|inline>` — the run configuration the pipeline declares.
- `--run-id <id>` — adopt a run identity; omit to let the framework assign one. Reusing an id resumes or extends
  that run, it does not silently start a second run under the same identity.
- `--cache reuse|refresh|bypass` — the run's cache policy (`CachePolicy`, `advanced-api/1-runners-and-clients.md`);
  defaults to `reuse`.
- `--resume` — continue a run that stopped before completion, from where it stopped.
- `--start-after <Phase>` / `--stop-after <Phase>` — execute a contiguous range of phases against recorded upstream
  state, by phase class.
- `--task <TaskClass>` / `--contract <ContractClass>` — exercise one task or one prompt contract in isolation, with
  the same recording a full run produces; `--model` supplies the model for a single contract.
- `--remote` — operate against the deployed runtime rather than in-process; identical command shape and result
  shape, differing only in placement. `ai-pipeline run --remote` launches the run on the runtime and follows it to
  its terminal state, returning the delivered outputs the same way a local run does. The launch primitive itself is
  non-blocking — a run is observable from the moment it is accepted — but the command waits for the result so the
  operator loop stays the same across placements; an operator that wants to launch without waiting uses the runtime
  surface directly (`runtime-api/2-run-control.md`).
- `--json` — emit the structured result: run identity, status, and the delivered outputs or terminal message.

## Output

The run identity and, on completion, the delivered output bundle; on failure or interruption, the recorded status
and the terminal message. The structured form carries the run identity, the status, and the outputs or error so an
agent can branch on the result and descend into the record with `ai-pipeline inspect`.

## Examples

Run a full pipeline locally (in-process, zero infrastructure) and against the deployed runtime — the same command,
differing only in placement:

```text
ai-pipeline run review_app --inputs ./inputs.json --run-config ./run-config.json
ai-pipeline run review_app --inputs ./inputs.json --run-config ./run-config.json --remote
```

Resume an interrupted run; completed work is reused, only incomplete work re-executes:

```text
ai-pipeline run review_app --resume --run-id review-2026-06-14
```

Re-execute one stage against recorded upstream state, paying only for that stage:

```text
ai-pipeline run review_app --run-id review-2026-06-14 --start-after CollectEvidence --stop-after AssessRisk
```

Exercise a single task in isolation with full recording:

```text
ai-pipeline run review_app --task AssessRiskTask --inputs ./assess-inputs.json --json
```

## Guarantees

- A run is validated before it starts, recorded as it proceeds, and bounded by its declared shape; a run the
  framework has proved cannot complete does not start (`3-guarantees.md § Pre-run validation`).
- A single-unit run is recorded exactly as it would be inside a full run — same spans, documents, provenance, and
  cache identity — and is inspectable and replayable identically; there is no faster, unrecorded path.
- Resume continues from where the run stopped without recomputing completed work or changing the meaning of prior
  results (`3-guarantees.md § Preserved, traceable, replayable runs`).
- The same command and result shape apply locally and against the runtime; a deployed pipeline accepts runs
  promptly, and the underlying launch is non-blocking even though `run --remote` waits for the result
  (`3-guarantees.md § Operational parity across placements`, `§ Non-blocking launch and prepared operation`).

## Does not guarantee

- That a run completes — a terminal failure stops it cleanly with a typed reason.
- That partial-range execution can run without the upstream documents its range requires already present in the
  record (`advanced-api/1-runners-and-clients.md`).
- A wall-clock latency; real-world speed depends on the work, the models, and where the runtime is deployed
  (`4-limits-and-non-promises.md § A latency or throughput guarantee`).
