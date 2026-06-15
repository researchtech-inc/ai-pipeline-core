# `ai-pipeline test`

Exercises a single pipeline unit — one task or one prompt contract — under the runner with the same recording a full
run produces, or regresses that unit against a recorded run, so an agent verifies behavior without re-running hours of
work. It is the pipeline-specific exercise surface, not the general test-suite runner: running the suite during
development goes through `dev test`, which adds lane policy, timeouts, parallelism, and the broader quality gate
(`testing/1-overview.md § The development quality surface`). `test` is the correctness gate of the loop: it *asserts*
that a unit behaves as expected and passes or fails. It is distinct from `ai-pipeline replay`, which *measures* a
change against recorded behavior and reports a comparison rather than a verdict — use `test` to gate, `replay` to
benchmark. `6-tools-and-the-development-loop.md` lists the whole loop. This file documents the command; how the tests
it exercises are *written* and structured is the `testing/` subtree (`testing/2-writing-tests.md`,
`testing/3-regression-and-benchmarking.md`).

## Synopsis

```text
ai-pipeline test <package> --select <unit-or-test> [--against-run <run-id>]
                          [--cache reuse|refresh|bypass] [--json]
```

## Options

- `<package>` — the application package the selected unit belongs to.
- `--select <unit-or-test>` — names the single unit to exercise: a named test, task, phase, or step. This is the
  command's selector — running the whole suite is `dev test` (`testing/1-overview.md`), not this command.
- `--against-run <run-id>` — exercise the selected units against the recorded inputs of a prior run (a recorded
  baseline, including a production run synced local with `ai-pipeline sync`), asserting recorded behavior still
  holds; this is regression testing over the recorded corpus.
- `--cache reuse|refresh|bypass` — the cache policy for the units the tests exercise; `reuse` lets unchanged
  upstream work be served from cache so only the unit under test re-executes.
- `--json` — emit the structured result: per-test pass/fail, the asserting message, and the recorded run identity
  of each exercised unit.

## Output

A pass/fail result per test, with each failure naming what was asserted and what was observed, and the run identity
of each exercised unit so its record is inspectable and replayable afterward. The structured form carries the
verdicts and run identities for automated consumption.

## Examples

Re-check a single unit after a change, against its cached upstream, and read the result as structured output:

```text
ai-pipeline test review_app --select AssessRiskTask --json
```

Target one step of a long pipeline without re-running the stages before it:

```text
ai-pipeline test review_app --select AssessRiskPhase --cache reuse
```

Regression-test selected units against the recorded inputs of a prior production run synced local:

```text
ai-pipeline sync review-prod-2026-06-10 --to local
ai-pipeline test review_app --select AssessRiskTask --against-run review-prod-2026-06-10 --json
```

## Guarantees

- A unit exercised by a test is recorded, inspectable, and replayable identically to its behavior inside a full
  run; a test does not get a faster, unrecorded path that diverges from production.
- Because units are exercised under the runner with content-addressed caching and resume, re-checking one unit, one
  step, or one phase costs the work of that unit, not a re-run of the whole pipeline
  (`3-guarantees.md § Preserved, traceable, replayable runs`). An agent gates a long pipeline one part at a time
  rather than paying for the whole run on every correction.
- A test may run against the recorded inputs of any prior run in the corpus, so the accumulated record — including
  production runs — is usable as a regression substrate.

## Does not guarantee

- That a passing test proves analytical correctness; it proves the unit behaves as the test asserts, and what the
  test asserts is the author's responsibility (`4-limits-and-non-promises.md`).
- That `test` performs the benchmark comparison itself. Measuring whether a cheaper model or a changed judgment is
  good enough — the expensive-baseline-versus-candidate loop — is `ai-pipeline replay` plus `ai-pipeline inspect
  --compare` (`tools/7-replay.md`, `6-tools-and-the-development-loop.md § The benchmarking loop`); `test` gates
  correctness, it does not score the comparison.
