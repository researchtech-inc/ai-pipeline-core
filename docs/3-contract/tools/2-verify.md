# `ai-pipeline verify`

Loads an application's declared package and validates its compiled run shape without running it. The application
declares its pipeline, phases, and tasks and ships no `__main__` or other entrypoint of its own; `ai-pipeline
verify` loads those declarations and proves their shape is executable. This is the cheapest point in the loop;
`6-tools-and-the-development-loop.md` lists the whole loop.

## Synopsis

```text
ai-pipeline verify <package> [--json]
```

## Options

- `<package>` — the application package to load and validate.
- `--json` — emit the structured result: a clean verdict, or the list of declaration errors, each with its
  location and concrete fix.

## Output

Either a clean result, or the set of declaration errors: unsatisfied inputs, incompatible outputs, invalid
selectors, unbounded repetition, mismatched role suffixes, missing paired body files, and the rest of the pre-run
validation surface (`3-guarantees.md § Pre-run validation`). Each error names what is wrong *and* the concrete fix.
The structured form is the same verdict and error list in machine-parseable shape, so an agent can correct the
package without scraping prose.

## Examples

Verify a package before spending a run:

```text
ai-pipeline verify review_app
```

Read the errors as structured output for automated correction:

```text
ai-pipeline verify review_app --json
```

## Guarantees

- Every error names what is wrong *and* the concrete fix. The reader is an agent correcting the package, so a
  diagnostic that does not say how to repair the package is itself a defect in the tool.
- It proves the package's topology, types, and configuration-independent shape — the declaration errors that do not
  depend on a concrete `RunConfig`. A package that verifies clean will not be rejected for one of those shape errors
  at plan-compile or run start (`3-guarantees.md § Pre-run validation`).

## Does not guarantee

- That a package that verifies clean produces a correct analysis; it proves the shape is executable, not that the
  judgments are right (`4-limits-and-non-promises.md`).
- Bounds and values that depend on a concrete `RunConfig` — for example a loop or fan-out bound supplied as a
  `RunConfig` field — are validated against the actual configuration by `ai-pipeline plan` and at run start, not by
  `verify`, which runs without a `--run-config` (`tools/3-plan.md`).
