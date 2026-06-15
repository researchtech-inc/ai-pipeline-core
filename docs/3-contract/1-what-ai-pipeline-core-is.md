# What ai-pipeline-core Is

ai-pipeline-core is the substrate for building long-chain, evidence-bearing analytical AI applications — systems
that make hundreds to thousands of dependent model-mediated judgments over documents and open-world material and
deliver conclusions someone will challenge. It is authored and operated by AI agents. The framework absorbs the
operational machinery every such application would otherwise rebuild — durable state, provenance, retries, pacing,
validation, caching, recording, replay — so an application carries only its domain meaning. The framework also
ships the runtime that executes a run and the surface to operate one from outside — launch it, observe it while it
runs, halt it, recover it, and read back its results, logs, and record — so neither the application nor the adopter
rebuilds the operating plane. It keeps the path from a declared application to a running, observed, and iteratively
improved one fast and uniform: the same one operator tool and the same surfaces operate a run identically whether it
runs locally where the work is developed or across many machines in production, so what is verified in development is
what ships.

This file orients a reader to what the framework is, who uses it, what it promises in one breath, and whether it
fits the work in front of you. There are four contract surfaces, and a reader uses the one that matches their role:

- **`api/`** — the authoring surface an agent uses to *write* a pipeline package, with the design steps that precede
  it in `authoring/`.
- **`advanced-api/`** — the in-process programmatic surface for code that *drives* a run and reads or writes its
  record within its own process.
- **`runtime-api/`** — the out-of-process surface an external system uses to operate a run over a boundary it did
  not author.
- **`tools/`** — the one operator command, `ai-pipeline`, with one subcommand per capability; the operator face of
  the two surfaces above.

Two guidance subtrees support those surfaces without being contract themselves: **`authoring/`** guides the design
steps that precede the `api/` code, and **`testing/`** guides how an application's tests are written against the
runner and how the recorded corpus is used for regression and benchmarking. Testing is a workflow over the existing
surfaces, not a new authoring role. Running those tests, the linters, and the type checker during development goes
through `dev`, a standalone external contributor tool that is the project's canonical quality surface — a companion to
these surfaces during development, not a fifth contract surface.

The remaining root files make the promise precise: the full guarantee list in `3-guarantees.md`, the boundaries in
`4-limits-and-non-promises.md`, and the configuration surface in `5-configuration.md`.

## Who Uses It

The framework serves four consumers at once, and the contract is written for all of them. (The mission groups
these as three: authoring and operating are two roles within one agent population, alongside the accountable human
and the external driving system.)

- **The authoring agent** writes the application: documents, prompt contracts, tasks, phases, and the pipeline that
  composes them. It relies on a small surface that is correct to use without reconstructing how the machinery
  underneath works.
- **The operating agent** runs, resumes, inspects, replays, tests, and measures the application during development
  and in production, through one tool — `ai-pipeline` — whose subcommands operate a run the same way wherever it
  executes, and through the in-process programmatic surface beneath that tool (`advanced-api/1-runners-and-clients.md`,
  `advanced-api/2-database.md`) when it drives or reads a run from inside its own process. It relies on the
  development loop being operable end to end without a human in the middle, on a fast change-to-result loop, and on
  the recorded corpus of past runs being a substrate it can re-measure to decide where a cheaper judgment suffices.
- **The accountable human reviewer** stakes a decision on the output and reviews conclusions, documents, and diffs.
  They rely on every delivered conclusion remaining explainable after the fact by someone who did not build the
  system.
- **The external driving system and its operators** launch a run, observe it while it runs, halt it when it must be
  stopped, recover it when execution or observation is interrupted, and read back its results, logs, and record —
  all from outside the process that executed it, through supported framework surfaces rather than by reaching into
  internals. It cannot import the application's or the framework's Python definitions, so it relies solely on the
  out-of-process runtime surface (`runtime-api/`), never on the in-process programmatic surface.

Application code is written by AI agents under human direction. Humans decide what the systems mean and carry that
intent through written specifications and review — never by editing application code by hand.

## The Central Promise

In one breath: you declare an application as documents, model-mediated judgments, execution steps, business stages,
and a pipeline that composes them; the framework validates that whole shape before any run starts, then executes it
while owning persistence, provenance, retry, pacing, caching, concurrency, and recording, and exposes supported
surfaces to launch, observe, halt, and recover a run from outside the process that executes it. You operate it
through one tool whose subcommands behave identically whether the run executes locally or in production, launching a
run from outside the process that executes it is non-blocking (the in-process `Runner.run` is, by design, a blocking
call), and re-entering one part of a long run costs the work of that part rather than a re-run of the whole.
Every document and every judgment is preserved in a durable record that an agent can run, resume, inspect, replay,
test, and measure — across the many runs already accumulated, so the record is a benchmark substrate as well as an
audit trail — that an external system can read back from outside the process that produced it, and that a reviewer
can trace from any delivered conclusion back to the evidence and judgments that produced it.

What the framework does not promise is that the conclusions are correct. It makes the work traceable, recoverable,
and reproducible in its mechanics; it does not make an analysis true. That boundary is the subject of
`4-limits-and-non-promises.md`, and you should read it before relying on the framework for anything that faces
challenge.

## The Five Authoring Roles

An application is built from five roles. This is orientation only; the authoring contract for each lives in `api/`.

- **Document** — durable, immutable, typed state with framework-owned identity and automatic provenance. The unit
  the framework persists and hands between steps. See `api/2-documents.md`.
- **PromptContract** — one model-mediated judgment, declared with its purpose, inputs, output model, and success
  criteria. See `api/4-prompt-contracts.md`.
- **Task** — one coherent execution step that turns resolved documents into durable document results. See
  `api/6-tasks.md`.
- **Phase** — one business stage that composes tasks and names the documents it hands forward. See
  `api/7-phases.md`.
- **Pipeline** — the complete declared run shape: ordered phases, gates, bounded loops, and the delivered output
  bundle. See `api/8-pipelines.md`.

Two supporting roles carry external capability: **model tools**, bounded capabilities the model may call during one
judgment (`api/5-model-tools.md`), and **service clients**, Python-owned external calls (`api/9-integrations.md`).
Content models — the typed values that cross these boundaries — are covered in `api/3-content-models.md`, and the
package shape that holds all of this is covered in `api/1-application.md`.

## Is This For You

The framework's strictness is justified by the forces of long-chain, evidence-bearing work, and only by them. Use
this short test to decide whether your work is in that class:

- **Strong fit:** long analytical chains of dependent judgments; document-heavy, evidence-sensitive work; open-world
  or adversarial inputs; outputs that face audit, review, or challenge; methodology stable enough to justify
  codification.
- **Poor fit:** conversational assistants, single-call extraction or classification, latency-sensitive interactive
  products, and prototypes that will never reach production scale.

Below the fit line the strictness costs more than it returns, and a lighter tool is the correct choice. This test
is a summary for deciding fit; the framework is built for long-chain, evidence-bearing analytical work where
durability, provenance, replay, bounded execution, and agent-operable correction are worth a strict authoring
model.
