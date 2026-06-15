# Coding Baselines

These are project-class code axioms. Exact commands, lint lanes, repository procedures, and release mechanics belong
to the implementation layer; these baselines constrain the code shape those later procedures carry out.

## Python 3.14 is the baseline

ai-pipeline-core uses modern Python as the baseline. Project-owned code should use current language features and type
syntax directly, without compatibility shims for older Python versions unless an accepted obligation requires them.
The force is machine-speed authorship: agents otherwise import legacy compatibility patterns from training priors,
fragmenting project-owned code and creating compatibility surfaces the project does not owe.

Do not introduce legacy typing style, transitional adapters, or compatibility layers merely because they are
familiar. If compatibility is required by a public promise or deployment obligation, make that obligation explicit
and keep the compatibility surface narrow.

## Typed frozen models carry structured boundaries

Structured values that cross framework boundaries are typed and immutable. Documents carry `FrozenBaseModel` content
for structured analytical state; prompt outputs, tool payloads, service payloads, polling observations, run
configuration, document bundles, and read-model values use explicit typed shapes rather than loose mappings.

Do not use `dict`, `Any`, mutable payloads, ad hoc JSON blobs, or stringly typed status fields for values that code,
agents, or external systems rely on. Closed routing values use closed types. Boundary fields carry descriptions
where the contract surface requires them, because future agents and generated prompts inherit field meaning from
those descriptions.

## Public I/O is asynchronous

Public I/O boundaries are async: task `run` methods, prompt execution, model tools, service clients, polling
clients, runner methods, and database reads and writes. Blocking or polling behavior is framework-owned and exposed
through typed async surfaces.

Application and framework code should not hide sleeps, local retry loops, throttle loops, synchronous network calls,
or blocking waits inside business logic. If work waits on an external system, the wait is represented through the
framework's typed retry, polling, run-control, or recovery surface.

## No mutable module-level business state

Module-level declarations may define classes, constants, type aliases, pure helpers, and the package-level
`pipeline` value. They must not hold run-affecting mutable state: caches, accumulators, registries, counters,
singletons, task progress, or cross-run memory.

This is the code-level instance of placement-independent correctness in `5-responsibility-boundary.md`: module scope
may declare stable code facts, not hidden business state.

## No unvalidatable derivatives

Do not hand-maintain string mirrors of typed facts when the relationship can be derived or validated from the source
of truth. Manual lists of role names, duplicated schema fields, copied output shapes, invented status strings,
parallel citation fields, and hand-written identity encodings drift faster than review catches them.

When a derivative is needed for a boundary, generate it from the typed owner or validate it against that owner before
it is accepted. If a derivative cannot be generated or validated and consumers rely on it, it is a new contract
surface and needs an owning answer rather than a convenient mirror.
