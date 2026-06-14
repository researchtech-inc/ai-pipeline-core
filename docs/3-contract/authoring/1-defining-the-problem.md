# Defining the Problem

This is the first authoring step: state what the application must produce and what kind of truth that output must
preserve, before choosing any solution shape. It is guidance for the author, not an enforceable surface — the
framework cannot decide your problem for you. `2-building-an-application.md` is the entry point that orders these
authoring files; this one owns the problem-definition step.

## Define the problem before the solution

Application design that begins with a workflow, a prompt, or a retrieval loop skips the harder question of what the
system must produce and what kind of truth that output must preserve. Define the problem first.

A problem statement names the decision or judgment the application supports, for whom, under what conditions, and
with what output — described by its role in a decision, not by its format. It stays valid even if the internal
implementation changes completely. Capture it as a written specification the rest of the application is derived
from; the application's design and code conform to it, not the other way around.

A good problem statement carries:

- **The supported decision.** What question the system helps answer, and what a trustworthy answer means.
- **Explicit scope.** What is in scope (handled, with success criteria), out of scope (the application surfaces an
  explicit signal rather than a silently wrong result), and deferred (relevant but intentionally not addressed,
  with a reason).
- **The compounding-risk surface.** Where the dependent judgments are, and where an early mistake can poison later
  work. Errors multiply through dependent steps across a long run, so name where checkpoints, review, or preserved
  ambiguity matter in your application's own judgments.
- **The change surface.** What is expected to vary — source types, criteria, questions, output expectations — and
  what stays stable enough to justify the structure.
- **The evidence environment and strategy.** For open-world work, state honestly how evidence behaves and how the
  application treats it: what counts as a lead versus confirmed evidence; when agreement is corroboration rather
  than echo; which inputs are adversarial; when a provisional conclusion may harden; what retrieval silence means;
  which findings must remain available to later judgments.
- **Success criteria** in three parts: correctness (what a correct output is), completeness (what sufficient
  coverage is), and honesty (how uncertainty, contradiction, and missing evidence are handled). When the output
  faces challenge or audit, traceability and replayability are explicit success criteria, not assumed side effects.
- **The delivery and operation surface.** Name who receives the result and how the run is operated. When a run is
  launched, watched, halted, recovered, or read by another system or an outside operator — not a person running it
  once — say so: the result must be understandable, and the run operable, by a consumer that did not build the
  application. State this at the level of what that consumer needs, not the mechanism that carries it.

## Confirm the work fits

Confirm the work fits the framework before designing it — the strong-fit and poor-fit signals are in
`1-what-ai-pipeline-core-is.md § Is This For You`. The framework pays for its strictness by surviving the forces of
long-chain evidence-bearing work; that strictness is wasted on work where those forces are weak. This step checks
whether the application needs the framework's strict durable, traceable, replayable execution model.

Needing to launch, observe, halt, recover, or read a run from outside the producing process does not make the work
a poor fit; operating a run from outside is part of the framework's supported surface, distinct from a
latency-sensitive, session-shaped product.
