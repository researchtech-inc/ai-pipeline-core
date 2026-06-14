# Decomposing the Run

This step turns an accepted problem into the framework's roles: a pipeline of phases, the tasks inside them, and
the documents that move between them. It is design guidance; the enforceable contract for each role lives in its
`api/` file, and the normative build order and package layout are in
`api/1-application.md § Dependency order points toward orchestration`.

## Answer five questions, in order

- What does one coherent run produce? → the **Pipeline** and its delivered output bundle.
- What business stages must occur inside that run? → the **Phases**.
- What atomic business actions occur inside each stage? → the **Tasks**.
- What durable documents remain after each stage? → the **Documents** and the phase handoffs.
- How is that run exposed and started? → the exported **pipeline** value the `ai-pipeline` operator command and the
  in-process runner load and run, with the run's root inputs supplied from outside; the application declares no
  entrypoint of its own.

A pipeline owns one run-shaped outcome and declares the maximum path that produces it. A phase is one business
stage a reader can name in a sentence. A task is one coherent business action with one honest input-and-output
contract — atomic by purpose, not by line count; it may contain several model calls or service calls if they serve
one deliverable. A document is durable state with independent meaning. The exported pipeline value is the one runnable
unit, and it is the same declared unit whether it is run locally through the `ai-pipeline` CLI, driven in-process
through the runner (`advanced-api/`), or deployed and called remotely through the runtime surface (`runtime-api/`) —
one declaration, operated the same way wherever it runs. The application declares it and nothing more.

## The application is declared, not launched

An application declares its run shape and nothing more: its content models, documents, prompt contracts, tasks,
phases, and the pipeline that composes them. The `ai-pipeline` operator command and the in-process runner load and
run the exported pipeline value, supplying the run's root inputs and its run configuration. The application writes no
`__main__`, no command-line entrypoint, no argument parser, and no manual run loop.

This is why earlier roles never import later orchestration to drive themselves, and why a task or phase never
parses arguments, reads the environment, or starts a run: execution belongs to the runner, not the application. A
design that seems to need a `main`, a CLI, or a hand-written run loop has the wrong shape — the missing piece is the
exported pipeline the framework loads and runs, not an entrypoint.

## Choose the smallest execution shape

Keep directly connected work inside one phase rather than splitting each task into its own phase. Choose the
smallest execution shape that fits the work — a sequential chain, a bounded fan-out, a bounded cross-product
fan-out over more than one axis, a serial fold, a small conditional, a bounded loop, or a best-effort fan-out that
records a typed degraded outcome for an item that fails and continues with the rest — rather than inventing new
control flow. If the work fits none of the standard shapes (`api/7-phases.md`, `api/8-pipelines.md`), that is a
signal the phase boundary or handoff design is wrong, not a reason to invent a new shape.

**Cross-product fan-out.** When work ranges over more than one axis at once — every entity against every scenario,
every candidate against every rubric, every investor against every startup — the shape is a bounded cross-product
fan-out, not a hand-built nested loop and not a flattening pass that materializes scaffolding documents only to
drive iteration (those fail the investigator test in `authoring/3-designing-the-document-model.md`). Each axis is
a bounded collection and the product is bounded by the declared maxima, so the maximum fan-out and cost stay
knowable before the run. The enforceable form is owned by `api/7-phases.md`.

**Best-effort fan-out.** When one item's terminal failure must not sink the whole collection — most items resolve
and one is unreachable — design the fan-out so the failed item yields a typed degraded-outcome document carrying
the reason and the rest continue; a later phase decides what the partial set means. This is distinct from
abstention, an accepted "insufficient evidence" answer the model returns, and from a terminal failure that must
stop the run. Decide deliberately which of the three you mean, because each carries a different downstream meaning.
The enforceable form is owned by `api/7-phases.md`.

## Model cross-entity work at two levels

When the application's value comes from comparing many entities, design two explicit levels of meaning:
entity-level work (bounded fan-out producing per-entity results) and cross-entity synthesis (phases that consume
those results and produce the population-level pattern as its own durable output). Cross-entity findings are a
different meaning with different consumers; do not let them stay trapped inside per-entity branches.

When the comparison is every entity against every other, or every entity against every scenario, the entity-level
work is a bounded cross-product fan-out and the synthesis phase consumes the resulting typed outcomes.
