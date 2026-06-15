# Testing an Application

This subtree is the guide to writing tests for an application built on ai-pipeline-core, sibling to `authoring/`
(which guides the design that precedes `api/` code). Testing is a workflow over the existing surfaces, not a new
authoring role: an application declares the five authoring roles in `api/`, and tests exercise those declarations
through the runner with the same recording a real run produces. There is no `api/` file for tests and no `*Test`
authoring role; a test is ordinary Python that drives the runner.

This file orients you to what testing is in this framework and which surface owns each part of it. The how lives in
`testing/2-writing-tests.md`; regression over recorded runs and the benchmark loop live in
`testing/3-regression-and-benchmarking.md`.

## What a test is here

A test exercises one unit — a task, a prompt contract, a contiguous phase range, or a whole pipeline — under the
runner, and asserts on the recorded result. Because the unit runs through the runner, it produces the same spans,
documents, provenance, cache identity, and replayability a full run produces; there is no faster, unrecorded test
path that could diverge from production (`advanced-api/1-runners-and-clients.md § Single units run with full
recording`). A test that bypasses the runner — calling a task's `run` method directly — leaves no record and is the
anti-pattern the runner contract rejects.

This is the same constraint the framework makes everywhere: the record is the substrate, and a test that does not
produce it cannot be inspected or replayed when it fails.

## The four surfaces testing stands on

Testing is not one surface but a workflow that composes four:

- **`advanced-api/1-runners-and-clients.md`** — the programmatic substrate a test stands on: the `Runner`, and its
  `run`, `run_range`, `run_task`, and `run_contract` methods that exercise a unit with full recording.
- **`advanced-api/2-database.md`** — the read seam a test reads recorded results, spans, and baselines through.
- **`tools/5-test.md`** — the `ai-pipeline test` operator command for pipeline-specific exercise: a single unit
  (`--select`) or regression against a recorded run (`--against-run`). It is the CLI face of the runner, not the
  authoring guide, and not how the suite is run during development (that is `dev test`, below).
- **`6-tools-and-the-development-loop.md § The benchmarking loop`** — how `test`, `replay`, and `inspect --compare`
  compose into the measurement loop.

`testing/` documents how a test is written and structured; `tools/5-test.md` documents the command that exercises one
unit through it.

## The development quality surface

During development, every quality check — linting, type checking, static analysis, and running the test suite — goes
through `dev`, a standalone external contributor tool (the `ai-dev-cli` package) that is the project's single
canonical quality surface. Raw quality tools — `pytest`, `ruff`, the type checker — are hook-blocked in the agent
environment; `dev` is the only sanctioned path for running them. All code and tests an application contributes are
expected to satisfy the development-quality baseline `dev` enforces — repository-wide requirements such as line
length 120 and strict linting and type checking, not only the test-shape constraints in `testing/2-writing-tests.md`
— whose durable rules are owned by `1-principles/7-coding-baselines.md`; `dev` is the surface that enforces them.

- `dev check` runs the ordered quality pipeline: bedrock static-analysis checks first, then the project's lint,
  typecheck, dead-code, semgrep, docstring, and test steps — the complete quality gate.
- `dev test` runs the suite through three lanes with distinct timeout, parallelism, and testmon policy: **unit** (the
  everyday development gate), **integration** (external-service-dependent tests), and **qualification** (long-running
  live validation). A test must satisfy the lane it lives in.

`dev` and `ai-pipeline` are complementary, not alternatives. `dev test` runs the test suite during development;
`ai-pipeline test` exercises one pipeline unit through the runner or regresses it against a recorded run
(`tools/5-test.md`). Tests are *written* against the runner (`testing/2-writing-tests.md`); they are *run* through
`dev test` during development and through `ai-pipeline test --select` for pipeline-specific targeting.

`dev` is not part of ai-pipeline-core and is not itself a contract surface; its commands, configuration, and hook
rules are contributor procedure. What is contract is the set of test-authoring constraints the quality pipeline
enforces, stated in `testing/2-writing-tests.md`.

## Where tests live

Application tests live in the package's `tests/` directory (`api/1-application.md`). That directory is package
layout, not a sixth authoring role: nothing in `api/` declares it, and a fresh agent locating the home for a test
finds it there. Tests import the runner surface from `ai_pipeline_core` and the application's units from the package.

## The three things tests do

1. **Assert behavior.** A behavior test exercises one unit and asserts the recorded result is what the author
   expects — the correctness gate. This is the everyday test, written per `testing/2-writing-tests.md`, run through
   `dev test` during development, and targetable to one unit through `ai-pipeline test --select`.
2. **Guard against regression.** A regression test exercises a unit against the recorded inputs of a prior run — a
   baseline, including a production run synced local — and asserts recorded behavior still holds
   (`testing/3-regression-and-benchmarking.md`, `tools/5-test.md`).
3. **Measure a change.** The benchmark loop re-executes recorded units under a different model or judgment and
   compares the recorded behavior; here `test` gates correctness while `replay` measures and `inspect --compare`
   compares (`testing/3-regression-and-benchmarking.md`, `tools/7-replay.md`).

The framework provides the substrate — durable inputs and outputs, unit re-execution, attributed cost. The
methodology that compares runs and the judgment of which result is better belong to the consuming agent, not the
framework (`4-limits-and-non-promises.md`).
