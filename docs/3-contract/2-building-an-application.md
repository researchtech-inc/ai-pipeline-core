# Building an Application

This is the entry point for authoring an application on ai-pipeline-core. A system that operates a run from outside
the process that runs it — launching, observing, halting, recovering, or reading it — uses the runtime surface
(`runtime-api/`) instead. This file gives the order in which the roles are designed and points into the
`authoring/` files that own each design step and the `api/` files that own each enforceable contract. It is guidance, not the normative reference: the package layout and build order are owned by
`api/1-application.md`, and each role's contract is owned by its `api/` file.

An application is authored from this contract alone. This file and `authoring/` make that surface sufficient for
design; `api/` makes it sufficient for code.

## The build order

Design and declare the roles in dependency order. Later roles consume earlier ones; earlier roles never import
later orchestration to define themselves. The normative order and package layout are in
`api/1-application.md § Dependency order points toward orchestration`.

1. **Content models** — the typed values that cross boundaries (`api/3-content-models.md`).
2. **Documents** — durable state built on those content models (`api/2-documents.md`).
3. **Model tools** — bounded model-callable capabilities, when a judgment needs them (`api/5-model-tools.md`).
4. **Prompt contracts** — the model-mediated judgments and their output models (`api/4-prompt-contracts.md`).
5. **Integrations** — service clients and polling clients, when a task needs external calls (`api/9-integrations.md`).
6. **Tasks** — execution steps that turn resolved documents into durable results, consuming the prompt contracts and
   service clients above (`api/6-tasks.md`).
7. **RunConfig** — the runner-supplied configuration the pipeline reads and passes into phases (`api/8-pipelines.md §
   RunConfig defines runner-supplied configuration`).
8. **Phases** — business stages that schedule tasks and name their handoffs (`api/7-phases.md`).
9. **Pipeline** — the complete run shape (`api/8-pipelines.md`).

## The design steps

Before that code exists, four design steps decide what it should be. Each is owned by one `authoring/` file:

1. **Define the problem** (`authoring/1-defining-the-problem.md`) — what the application must produce and what kind
   of truth its output preserves, before any solution shape, and a check that the work fits the framework.
2. **Decompose the run** (`authoring/2-decomposing-the-run.md`) — turn the problem into a pipeline of phases,
   tasks, and document handoffs, choosing the smallest execution shape that fits.
3. **Design the document model** (`authoring/3-designing-the-document-model.md`) — decide which state is durable,
   what each document means, how provenance is recorded, and how ambiguity is preserved.
4. **Plan the model interactions** (`authoring/4-planning-model-interactions.md`) — decide which judgments need a
   model, what each sees and returns, and how its prompt body operationalizes the question.

## Run, inspect, and correct

Authoring is one half of the loop; operating the application is the other, and the framework is built so an agent
operates it without a human in the middle, through one tool — `ai-pipeline`. After you declare an application you
`verify` it compiles, `plan` its shape and cost, `run` it (whole, partial, or a single unit) and `test` its units,
`inspect` the record, `replay` units to measure changes, and `deploy` it; you then operate the deployed run with the
same commands and observe it through the runtime surface. The loop runs locally with no external infrastructure and
operates a deployed run the same way, differing only in placement: what you verify in development is what ships.
Quality gating during development — linters, type checking, and the test suite — runs through `dev` (`dev check` and
`dev test`), the project's canonical quality surface, while `ai-pipeline test` exercises a single pipeline unit or
regresses it against a recorded run (`testing/1-overview.md`).
Because production runs are long and expensive, the everyday actions are resume, partial-range re-execution, and
replay against the cache — re-entering one part of a run, not re-running the whole — and the accumulated record is a
benchmark substrate you re-measure to decide where a cheaper judgment suffices.

That surface — the development loop — is `6-tools-and-the-development-loop.md` for the operator view (the
`ai-pipeline` CLI) and `advanced-api/1-runners-and-clients.md` (with `advanced-api/2-database.md`) for the in-process
programmatic run and read surface; operating a run from outside the process that ran it is the runtime surface
(`runtime-api/`). Writing the application's tests — exercising its units through the runner, and using the recorded
corpus for regression and benchmarking — is the `testing/` subtree (`testing/1-overview.md`).
