# `ai-pipeline init`

Scaffolds a fresh application package in the fixed layout the framework expects, so a new project starts from a
correct, loadable shape rather than a hand-assembled one. This is the first step of the development loop;
`6-tools-and-the-development-loop.md` is the entry point that lists the whole loop.

## Synopsis

```text
ai-pipeline init <package-name> [--path <dir>] [--json]
```

## Options

- `<package-name>` — the package to scaffold; becomes the package root and the importable name.
- `--path <dir>` — where to create the package; defaults to the current directory.
- `--json` — emit the structured result (created paths, the package name, the exported `pipeline` symbol).

## Output

The scaffolded package and a summary of what was created. The scaffold is the package layout that
`api/1-application.md` makes normative: the always-present structure — `documents/`, `prompt_contracts/`, `tasks/`,
`phases/`, `tests/`, `run_config.py`, `pipeline.py`, and an `__init__.py` that exports one `pipeline` value — plus
the role directories for the optional roles named in that layout (`tools/` for model tools, `integrations/` for
service clients, `pipelines/` for child pipelines), created when the role first appears. The structured form lists
the created paths and the exported symbol so an agent can proceed without re-reading the tree.

## Examples

Scaffold a new package in the current directory:

```text
ai-pipeline init review_app
```

Scaffold into a chosen directory and read the result as structured output:

```text
ai-pipeline init review_app --path ./apps --json
```

## Guarantees

- The scaffolded package is well-formed for `ai-pipeline verify`: it exports one `pipeline` value and declares no
  run entrypoint of its own (`api/1-application.md`).
- The layout matches the fixed package layout, so a fresh agent can locate the home for any role from the path
  alone.

## Does not guarantee

- That the scaffold contains a working analysis — it is structure, not domain logic. Authoring the documents,
  prompt contracts, tasks, phases, and pipeline is the author's work (`api/`, `authoring/`).
