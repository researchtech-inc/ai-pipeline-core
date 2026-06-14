# `ai-pipeline replay`

Re-executes recorded units against their recorded inputs, with optional overrides, to measure the effect of a
change. This is the application's benchmarking surface: where `ai-pipeline test` *asserts* a unit behaves as
expected, `replay` *measures* how a changed judgment, body file, or model selection compares against recorded
behavior. It is the operator face of the replay surface in `advanced-api/`. `6-tools-and-the-development-loop.md`
lists the loop.

## Synopsis

```text
ai-pipeline replay <span-id> [--model <AIModelRef>] [--set <key=value>...] [--json]
ai-pipeline replay --from-run <run-id> [--kind <unit-kind>] [--model <AIModelRef>] [--concurrency <n>] [--json]
```

## Options

- `<span-id>` — the recorded unit to re-execute against its recorded inputs.
- `--from-run <run-id>` — replay a batch of recorded units from a run, optionally filtered by `--kind` (the unit
  kind: `pipeline`, `phase`, `task`, or `judgment`); targeting a recorded production run measures a change across
  real traffic.
- `--model <AIModelRef>` — override the model for the replay, the lever for cheaper-model benchmarking.
- `--set <key=value>` — override a recorded input or option for the replay.
- `--concurrency <n>` — how many units a batch replays at once.
- `--json` — emit the structured comparison: the re-executed result and the recorded behavior it is measured
  against (cost, tokens, duration, output).

## Output

The re-executed result and the recorded behavior to compare it against. The structured form is the comparison —
recorded vs replayed cost, tokens, duration, and output — so an agent can decide, per judgment, where a cheaper
model suffices. `ai-pipeline inspect --compare` assembles the same comparison as a readable bundle.

## Examples

Re-run one recorded judgment under a cheaper model and read the comparison:

```text
ai-pipeline replay 9KQ2-prompt-exec-3 --model cheap-model --json
```

Benchmark a cheaper model across every recorded judgment of a recorded production run:

```text
ai-pipeline replay --from-run review-2026-06-14 --kind judgment --model cheap-model --concurrency 5 --json
```

## Guarantees

- Deterministic code re-executes faithfully against recorded inputs, and a measurement is made against recorded
  behavior rather than against a fresh, unrelated run.
- Replay pays only for the unit it re-executes, not for the whole run, so studying or measuring one part of a long
  run costs the work of that part (`3-guarantees.md § Preserved, traceable, replayable runs`).
- The framework supplies the substrate — re-execution against recorded inputs and cost attribution; the comparison
  methodology and the judgment of which result is better are the consuming agent's
  (`4-limits-and-non-promises.md`).

## Does not guarantee

- That a replayed model judgment reproduces the original token-for-token. Replay re-samples model rounds; a
  difference between the replayed and original output is the signal replay exists to surface, not an error
  (`4-limits-and-non-promises.md § Replay as bit-identical reproduction`).
