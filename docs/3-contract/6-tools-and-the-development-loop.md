# The `ai-pipeline` CLI and the Development Loop

The framework ships one operator command — `ai-pipeline` — that an agent uses to operate an application, not only to
author it. The development loop — scaffold, verify, preview, run, test, inspect, replay, correct, deploy, observe,
recover — is operated by an agent with no human required in the middle: the runtime-facing work through this one
tool, and linting, type checking, and the test suite through the companion `dev` quality surface (described below).
This file
is the entry point to that surface; the per-subcommand contracts live one file per subcommand in `tools/`, each
documenting what the subcommand is for, its synopsis and options, its output (human-readable and the structured
form), worked examples, and what it does and does not guarantee. It documents behavior, not implementation.

Two boundaries scope this surface. First, `ai-pipeline` is the *operation* surface, not the code-quality surface.
During development the test suite, linters, type checker, and static analysis run through `dev` — a standalone
external contributor tool (the `ai-dev-cli` package) that is the project's single canonical quality surface, with raw
quality tools hook-blocked in the agent environment. The two are complementary: `ai-pipeline` operates a run — plan,
run, inspect, replay, cancel, recover, deploy, and the pipeline-specific unit exercise of `ai-pipeline test` — while
`dev check` runs the quality gate and `dev test` runs the suite. `dev`'s commands, configuration, hook rules, and
lane mechanics are contributor procedure, not part of this operator contract; what is contract is the set of
test-authoring constraints the quality pipeline enforces (`testing/2-writing-tests.md`).
Second, the programmatic contract for running and reading a pipeline in-process is owned by
`advanced-api/1-runners-and-clients.md` and the database API in `advanced-api/2-database.md`; `ai-pipeline run`,
`ai-pipeline test`, `ai-pipeline inspect`, `ai-pipeline replay`, and `ai-pipeline sync` are the operator-facing face
of that same in-process surface, and they share its guarantees. The surface an external system uses to launch,
observe, halt, recover, and read a run from outside the process that ran it is the runtime surface (`runtime-api/`);
`ai-pipeline cancel`, `ai-pipeline recover`, and inspecting a run across a boundary stand on that surface.

## One tool, one subcommand per capability

There is exactly one operator command. Its subcommands are a closed set:

| Subcommand | What it does | Contract |
| --- | --- | --- |
| `ai-pipeline init` | Scaffold a fresh application package in the fixed layout | `tools/1-init.md` |
| `ai-pipeline verify` | Compile and validate a package without running it | `tools/2-verify.md` |
| `ai-pipeline plan` | Preview the run shape, fan-out, and cost ceiling before spending | `tools/3-plan.md` |
| `ai-pipeline run` | Execute whole, partial, or a single unit; resume an interrupted run | `tools/4-run.md` |
| `ai-pipeline test` | Exercise one pipeline unit under the runner with full recording, or regress it against a recorded run | `tools/5-test.md` |
| `ai-pipeline inspect` | Read a run's status, outputs, spans, logs, cost, and provenance; build a trace bundle | `tools/6-inspect.md` |
| `ai-pipeline replay` | Re-execute recorded units with overrides to measure a change — the benchmarking surface | `tools/7-replay.md` |
| `ai-pipeline cancel` | Halt a run in flight without corrupting its record | `tools/8-cancel.md` |
| `ai-pipeline recover` | Reconcile a crashed or interrupted run to its recorded truth | `tools/9-recover.md` |
| `ai-pipeline sync` | Move run records between local and shared stores | `tools/10-sync.md` |
| `ai-pipeline deploy` | Package and publish an application, and stand up the supported runtime topology | `tools/11-deploy.md` |

`init` and `test` are first-class subcommands; `inspect` includes the markdown trace-bundle workflow (there is no
separate trace tool); single-unit execution is a mode of `run` and the substrate `test` stands on, not a separate
command. There is deliberately no `serve` subcommand (see _Local execution has two profiles_ below). `ai-pipeline
test` exercises a single pipeline unit or regresses it against a recorded run; running the test *suite* during
development is `dev test`, and how tests are *written* against the runner is the `testing/` subtree
(`testing/1-overview.md`).

## Two operating principles

- **Single-command operations.** Each capability is one `ai-pipeline` subcommand. The operator assembles no
  orchestration by hand and wraps the framework in no per-application scripts; a fragmented operating surface is the
  operating-side version of the shape divergence this frame exists to prevent.
- **Operation does not bifurcate by placement.** The same subcommands operate a run identically whether it executes
  locally in one process or remotely across many. Targeting production rather than a local runtime is a
  configuration change (`5-configuration.md`), not a different tool, a different command, or a different mental
  model.

## Structured output for an agent operator

Every subcommand emits a structured, machine-parseable form alongside its human-readable output, and every error it
reports names the concrete fix. The operator is an AI agent: it reads `run`, `test`, `inspect`, and `replay` output
programmatically to drive the loop and the benchmark workflow below. The structured form and the fix-naming are the
contract; the precise flag that selects the structured form is a surface detail.

## Local execution has two profiles

- **In-process, zero infrastructure.** `verify`, `plan`, `run`, `test`, `replay`, and `inspect` run on one machine
  with no external services, filesystem-backed, producing the same record a full run produces. This is the everyday
  authoring-and-correction loop.
- **Local full stack.** Full runtime/API parity locally is achieved by standing up the supported runtime where the
  work is developed and `ai-pipeline deploy`-ing to it, then operating it through the same runtime surface used in
  production. There is no `serve` subcommand: the HTTP runtime always runs as the supported stack, local or remote,
  and is reached the same way in both.

## The loop

A normal cycle, with the subcommand at each step:

1. `ai-pipeline init` — scaffold the package.
2. Author the package (documents, prompt contracts, tasks, phases, the pipeline) per `api/`.
3. `ai-pipeline verify` — the cheapest gate; every declaration error names its fix.
4. `ai-pipeline plan` — preview shape, fan-out, and the cost ceiling before spending.
5. `ai-pipeline run` — execute locally; for a long run, re-enter from a single unit or a contiguous phase range
   against recorded upstream state rather than re-running the whole thing.
6. `dev test` / `dev check` — run the test suite and the quality gate during development; `ai-pipeline test`
   exercises a single pipeline unit under the runner with full recording, or regresses it against a recorded run.
7. `ai-pipeline inspect` — read status, outputs, spans, the unified logs, cost, and provenance; trace a conclusion
   to its evidence.
8. `ai-pipeline replay` — re-execute recorded units with overrides to measure whether a change helped.
9. `ai-pipeline deploy` — publish and stand up the runtime.
10. `ai-pipeline run` (against the runtime) plus `ai-pipeline cancel` / `ai-pipeline recover` — operate the deployed
    run; observe it through the runtime surface (`runtime-api/`).

Because production runs take hours and spend real money, the everyday actions are **resume, partial-range execution,
and replay against the cache** — re-entering and re-measuring one part of a run, not re-running everything. The cost
of observing the effect of a change is proportional to the change, not to the size of the run
(`3-guarantees.md § Preserved, traceable, replayable runs`, `§ Model routing, caching, and concurrency`).

## The benchmarking loop

The durable record of past runs — including production runs — is a reusable benchmark substrate. The supported
workflow composes the subcommands above:

1. Establish a baseline by running pipelines with strong models; the record preserves each run's inputs and outputs.
2. `ai-pipeline replay` re-executes recorded units, or whole runs, on cheaper models against the recorded inputs —
   per-unit, in batch, or targeted at recorded production runs; `ai-pipeline sync` pulls production records local to
   measure offline. Replay is the measurement surface; `ai-pipeline test` stays the correctness gate (it asserts a
   unit still behaves as expected, including in regression against a recorded run, rather than scoring a model
   swap).
3. `ai-pipeline inspect --compare` compares the recorded behavior — cost, tokens, and output — of the baseline and
   the candidate.
4. An agent decides, per judgment, where the cheaper model suffices and where it does not.

The framework provides the substrate — durable inputs and outputs, unit re-execution, cost attributed to the
judgment. The methodology that compares runs and the judgment of which result is better belong to the consuming
agent, not the framework (`4-limits-and-non-promises.md`).
