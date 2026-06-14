# `ai-pipeline plan`

Renders the compiled run shape and previews its cost and fan-out before anything executes.
`6-tools-and-the-development-loop.md` lists the whole loop.

## Synopsis

```text
ai-pipeline plan <package> --run-config <path|inline> [--json]
```

## Options

- `<package>` — the application package whose run shape to compile.
- `--run-config <path|inline>` — the run configuration to compile the plan against; bounds (loop rounds, fan-out
  limits) come from here and from the declared shape.
- `--json` — emit the structured plan: the ordered phases, the fan-out and loop bounds, and the cost ceiling.

## Output

The declared maximum path and the bounds on it: the phases, tasks, bounded loops, and gates the run can traverse;
how many items each fan-out can process; how many rounds each loop can run; and the cost ceiling those bounds imply.
The structured form carries the same shape and bounds so an agent can decide whether to spend the run.

## Examples

Preview shape and cost before committing money:

```text
ai-pipeline plan review_app --run-config ./run-config.json
```

Read the bounds as structured output:

```text
ai-pipeline plan review_app --run-config ./run-config.json --json
```

## Guarantees

- The previewed shape is the shape the run executes within — runtime skips unused paths but never invents new ones,
  so the preview is an upper bound, not a guess (`3-guarantees.md § Bounded execution`).
- The cost ceiling follows from declared, enforced bounds, so the maximum spend of a run is knowable before it
  starts (`3-guarantees.md § Cost recording and attribution`).

## Does not guarantee

- The actual cost of a run, which depends on which paths execute and what the models do; it bounds the maximum, it
  does not predict the realized figure.
