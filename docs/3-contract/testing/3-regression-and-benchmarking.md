# Regression and Benchmarking

This file covers the two workflows that read the accumulated record of past runs: regression testing a unit against
a recorded baseline, and the benchmark loop that measures whether a changed judgment or a cheaper model is good
enough. Both treat the durable record — including production runs — as a reusable substrate. The framework provides
that substrate; the methodology that compares runs and the judgment of which result is better belong to the
consuming agent (`4-limits-and-non-promises.md`).

## Regression against a recorded baseline

A regression test exercises a unit against the recorded inputs of a prior run and asserts that recorded behavior
still holds. The prior run can be any run in the corpus, including a production run pulled local with
`ai-pipeline sync` (`tools/10-sync.md`). This is how the accumulated record becomes a regression substrate rather
than only an audit trail.

The operator face is `ai-pipeline test --against-run <run-id>` (`tools/5-test.md`); programmatically, a test reads
the baseline run's recorded inputs through the read seam (`advanced-api/2-database.md`) and re-exercises the unit
through the runner, asserting on the recorded result.

- A regression test asserts the unit *still behaves as expected* against recorded inputs; it is a pass/fail gate,
  not a measurement.
- A regression test exercises the unit through the runner with full recording, so its record is inspectable and
  replayable exactly as a fresh run's is.
- A regression test reuses cached upstream work where the cache key matches, so re-checking one unit costs the work
  of that unit, not a re-run of the whole pipeline (`3-guarantees.md § Preserved, traceable, replayable runs`).

## The benchmark loop

The benchmark loop measures whether a change — a different model, a revised body file, a changed judgment — is good
enough, by re-executing recorded units against their recorded inputs and comparing the recorded behavior. It
composes three commands, each with a distinct job:

1. **`ai-pipeline replay`** re-executes recorded units (one, a batch, or whole runs) against their recorded inputs
   with overrides — typically a cheaper model — and reports the comparison: recorded versus replayed cost, tokens,
   duration, and output (`tools/7-replay.md`). Replay *measures*.
2. **`ai-pipeline inspect --compare`** assembles that comparison as a readable bundle for a baseline and a candidate
   (`tools/6-inspect.md`).
3. **`ai-pipeline test`** stays the correctness *gate*: it asserts a unit still behaves as expected, including in
   regression against a recorded run, rather than scoring a model swap (`tools/5-test.md`).

A typical loop: establish a baseline by running pipelines with strong models; the record preserves each run's
inputs and outputs. Replay the recorded units on cheaper models against the recorded inputs — per-unit, in batch, or
targeted at recorded production runs synced local. Compare the recorded behavior. An agent then decides, per
judgment, where the cheaper model suffices and where it does not.

## The bright line

`test` gates correctness; `replay` measures a change; neither decides whether a result is *good*. The framework
supplies the substrate — durable inputs and outputs, unit re-execution against recorded inputs, cost attributed to
the judgment — and the comparison methodology and the judgment of which result is better are the consuming agent's,
not the framework's (`6-tools-and-the-development-loop.md § The benchmarking loop`,
`4-limits-and-non-promises.md`). Reading the recorded corpus programmatically for this measurement is the read seam
in `advanced-api/2-database.md`; the out-of-process projection of the same reads is `runtime-api/4-run-record-read-model.md`.
