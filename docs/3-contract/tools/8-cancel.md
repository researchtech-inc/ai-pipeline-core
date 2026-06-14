# `ai-pipeline cancel`

Halts a run in flight, from outside the process that runs it, without corrupting its record. It is the operator face
of the governable-run capability the runtime surface also exposes to an external driving system
(`runtime-api/2-run-control.md`). `6-tools-and-the-development-loop.md` lists the loop.

## Synopsis

```text
ai-pipeline cancel <run-id> [--json]
```

## Options

- `<run-id>` — the run to halt.
- `--json` — emit the run's recorded terminal state once the halt takes effect.

## Output

The run's recorded terminal state once the halt takes effect — a recorded `CANCELLED`, not a crash. The structured
form carries the run identity and the terminal state.

## Examples

Halt a run whose inputs turned out wrong, without killing the process or machine that holds it:

```text
ai-pipeline cancel review-2026-06-14
```

## Guarantees

- The halt stops further work and spending without killing the process and without corrupting what was already
  recorded; a halted run ends as a recorded terminal state, not as a crash, and what it produced before the halt
  stays intact and readable (`3-guarantees.md § Governable in-flight runs`).

## Does not guarantee

- That the halt is instantaneous: work already in flight when the signal arrives may finish and be recorded before
  the run stops (`4-limits-and-non-promises.md § Instantaneous cancellation`). It does not roll back work already
  completed and recorded.
