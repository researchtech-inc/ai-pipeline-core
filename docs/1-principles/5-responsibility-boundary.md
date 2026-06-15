# Responsibility Boundary

## Framework absorbs generic complexity; applications carry domain meaning

The framework owns the generic machinery every long-chain evidence-bearing application would otherwise rebuild:
durable state, provenance, identity, validation, retry, pacing, caching, bounded execution, recording, replay,
inspection, cost attribution, deployment topology, and the surfaces for launching, observing, halting, recovering,
and reading a run.

Applications own domain meaning: the problem they solve, their evidence methodology, source-reliability policy,
scoring rules, thresholds, rubrics, prompt bodies, and what their conclusions mean. Application code should be about
those meanings, expressed through the framework's roles, not about reconstructing generic operation.

When an application repeatedly needs local persistence, retry, routing, observation, run control, state recovery,
trace assembly, or benchmark plumbing, treat that as evidence of a framework gap unless the need is truly
domain-specific.

## Author on the correct side of the bright line

The line is substrate versus methodology. The framework supplies the substrate that makes analytical work durable,
bounded, inspectable, and operable. It does not decide domain truth or absorb products that reason over many runs.

Keep domain epistemics in applications or adjacent products. Keep cross-run scoring, living assessments,
warehousing, dashboards, authentication and authorization products, user or tenant security policy, and downstream
trust-assessment products outside the framework. Keep the framework strict where strictness protects the forces it
owns, and do not expand it into downstream products merely because the record makes them possible.

## Strictness must name the force it answers

The framework is strict because the problem frame's forces make flexibility dangerous: long-chain compounding,
machine-speed authorship, evidence loss in transit, unreliable execution, external operation, and the need to recover
from the durable record. Strictness that cannot name one of those forces is cost, not safety.

Before adding a new rejection, validation, fixed shape, required field, closed set, or supported-only path, name the
force it protects and why the existing surface does not already protect it. Remove or narrow strictness when its
force is gone, duplicated elsewhere, or below the framework's fit boundary.

## What crosses a system boundary is a contract; operate through supported surfaces

Anything parsed or relied on across a boundary is a contract whether or not it was meant to be one. Authoring APIs,
the advanced in-process runner and database seam, runtime interop shapes, CLI structured output, document read
models, lifecycle events, run states, and persisted records must therefore be deliberate, versioned where external,
and able to fail loudly when a reader meets an unsupported shape.

Ordinary users operate through supported surfaces. Application authors use `api/`. In-process drivers and tests use
`advanced-api/`. External systems use `runtime-api/`. Operators use `ai-pipeline`. Reaching into framework internals
because the supported surface is inconvenient creates an undocumented contract and freezes the machinery beneath it.

## No divergent local mode; correctness is independent of placement and process-local state

Local development and production are two placements of one system, not two systems. The same commands, record,
runtime concepts, replay, inspection, recovery, and supported surfaces apply locally and remotely; only configuration
and capacity differ.

Correctness must not depend on where work runs or which process held memory. Tasks depend on resolved documents,
declared fields, service-client boundaries, and the durable record. They do not depend on mutable module globals,
sibling tasks sharing a process, local caches carrying business state, current working directory side effects, or
hidden in-memory progress. Tests run through the runner with full recording; there is no faster unrecorded test path.

Process-local transport infrastructure may live behind integration boundaries when it is plumbing only. Business
state that must survive leaves as a document or receipt.

## No compatibility scaffolding without accepted obligation

Compatibility is preserved for real obligations: accepted contracts, stored records, public schemas, audit trails,
evaluation baselines, delivered artifacts, integrations, security or privacy commitments, and explicit maintainer
requirements. Outside those obligations, project-owned surfaces move to the accepted shape directly.

Do not preserve parallel old and new APIs, aliases, fallback paths, legacy examples, wrapper commands, or migration
shims for comfort. In this project, parallel valid shapes are not harmless; they are copied by agents and become a
new axis of divergence.
