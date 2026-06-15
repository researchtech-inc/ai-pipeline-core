# ai-pipeline-core Documentation

This README orients a new reader. It tells you what this corpus is, how authority flows through it, and where
to start. It is not the product contract and not the problem frame. When this README and the numbered layers
differ, the numbered layers govern.

## What this is

`docs/` is the doctrine for ai-pipeline-core: the current accepted intent that governs how the framework is
specified, designed, built, and maintained. It is written to be loaded as working context by the AI agents and
humans who do that work, not kept as passive reference material. Code, tests, prompts, schemas, and generated
outputs are artifacts below this corpus: they execute and verify doctrine, and when they drift from it, the
divergence is a defect in one of the two — never a silent redefinition of intent.

## The layers

Doctrine is organized into five layers. Lower numbers carry higher authority; each later layer answers a
narrower question while preserving what earlier layers accepted.

- `1-principles/` — How must humans and AI agents work on this project class? Durable behavioral rules,
  coding baselines, and hard mandates, each paired with the pressure that makes it necessary.
- `2-problem/` — What about the world makes ai-pipeline-core necessary? Mission, constraints, hard parts,
  and non-goals. Environmental forces only: no mechanisms, no framework vocabulary, no behavioral commands.
- `3-contract/` — What may an application author rely on? The public authoring surface, the guarantees the
  framework owns, the limits it does not promise past, configuration, and user-facing tooling.
- `4-design/` — How does the framework satisfy the contract internally? Chosen mechanisms, internal
  boundaries, rejected alternatives, and the rationale needed to change any of it safely.
- `5-implementation/` — How do contributors carry the design into code the same way every time? Repository
  shape, development workflow, testing doctrine, release procedure.

Discovery can start anywhere — a failing test can reveal a design flaw, and a design dead end can reveal a
problem-frame mistake — but the accepted answer lives in the layer that owns the question, and repairs land
there first. The binding form of the authority rules — how conflicts resolve, what counts as duplication, how
layers may change — belongs to the principles layer; this README only orients.

## Current state

`2-problem/` is the accepted problem frame, and `3-contract/` is present: its authoring surface (`api/`) with its
design guidance (`authoring/`) and test-authoring guidance (`testing/`), the operator surface (`tools/`), the
in-process programmatic seam (`advanced-api/`), and the out-of-process runtime surface (`runtime-api/`) are written.
`1-principles/`, `4-design/`, and
`5-implementation/` are not yet present in this corpus; prior documentation for those layers is source and
evidence, not current authority. Until the principles and implementation layers land here, the root
`CLAUDE.md` remains the operative standard for code work.

The contract is written in present tense as the 0.3.0 target, so some of it describes behavior the implementation
is still being built to satisfy. The append-only / never-delete database invariant, the halt-on-recording-failure
rule, and the recording substrate's throughput floor are recorded as planned 0.3.0 behavior in
`3-contract/_decisions.md § The database is append-only and authoritative, with three profiles`, not as
already-shipped behavior.

## Where to start

If you are deciding whether proposed work serves the project, read `2-problem/` — its four files are one
frame and load together. If you are changing the framework itself, load the problem frame plus whichever
layers govern your change. If you are authoring an application on the framework, the contract layer is your
surface.
