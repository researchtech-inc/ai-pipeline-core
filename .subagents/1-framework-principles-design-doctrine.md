# Framework Principles and Design Doctrine

## Purpose

ai-pipeline-core exists to make complex AI pipelines easy to build, easy to read, and hard to get wrong. The framework is not meant to expose its internal complexity to application authors; it is meant to absorb that complexity so application code can stay focused on business logic.

This doctrine defines the quality bar for the framework and for code written on top of it. It explains what “good” looks like, what the framework must do automatically, what application code should and should not be responsible for, and how to judge whether a pattern belongs in the system.

---

## 1. Framework Absorbs Complexity, Applications Stay Simple

The central promise of the framework is that application developers should mostly write business logic, not infrastructure glue. Tracing, persistence, retries, observability, debugging support, execution tracking, provenance capture, and other operational mechanics are framework responsibilities.

If an application author must manually wire persistence, think about database writes, manage tracing, or carry bookkeeping values just so the system works, the framework has failed its purpose. Application code should read like: “what problem is being solved, in what order, and with what inputs and outputs,” not “how do I appease the runtime.”

A good rule is this: if a concern is generic across pipelines, the framework should own it. If a concern is domain-specific, the application should own it. The framework should be doing the heavy lifting so that business logic is the only thing left visible in application code.

**Example**
A developer should not need to know when documents are persisted, how spans are written, or how traces are assembled. They should only need to know what a flow does, what a task accepts, and what a task produces.

---

## 2. Poka-Yoke: Prevention Over Detection, Detection Over Correction

The framework explicitly follows a Poka-Yoke methodology. The goal is not merely to detect mistakes; it is to structure the system so the most common mistakes are difficult or impossible to make in the first place.

Warnings and runtime checks have value, but they are not the primary line of defense. A warning still assumes the developer has already been allowed to write the wrong thing. The preferred order is:

1. Prevent the mistake structurally.
2. If prevention is not possible, detect it at definition time or import time.
3. If that is not possible, detect it at runtime immediately and fail clearly.
4. Avoid “silent correction” unless the correction is guaranteed to preserve intent.

This doctrine matters especially for AI-generated code. If the framework exposes a loophole, an AI agent will eventually use it. “Discouraged” is not strong enough. The framework must guide code toward the correct path by making that path the easiest one and making the wrong path awkward or unavailable.

**What this means in practice**
- Prefer changing the authoring model over adding more warnings.
- Prefer replacing a misuse-prone API with a safer API over documenting “please don’t do that.”
- Prefer one correct pattern over several flexible but ambiguous ones.

---

## 3. Hide Mechanics, Not Meaning

The framework should hide operational mechanics from users, but it must not hide business meaning. This distinction is crucial.

Good things to hide:
- persistence
- tracing
- retry machinery
- execution bookkeeping
- artifact storage
- provider client setup
- cache and runtime plumbing

Bad things to hide:
- what data a task depends on
- what a flow needs to run
- what a task produces
- why a branch happens
- which step is responsible for a decision

The framework should make infrastructure invisible, but dependencies explicit. A developer should never worry about storage internals, but they should always be able to tell, from code alone, what a task or flow needs and what it emits.

This is why “magic” that obscures data dependencies is dangerous even when convenient. If code becomes shorter by making contracts less truthful, it is worse, not better.

**Good**
A task’s signature makes its inputs obvious.

**Bad**
A task or flow silently reaches into ambient state, a graph, or a hidden document pool to fetch undeclared dependencies.

---

## 4. Strong Contracts Are the Source of Readability

The framework must optimize for code that can be understood locally. A reader should be able to open a task or a flow and understand what it does by reading:
- its name
- its inputs
- its outputs
- the document types it works with

If understanding a step requires chasing hidden conventions, reverse-engineering helper functions, or reading the producer of every upstream artifact, the design is too indirect.

Strong contracts are not only a type-safety feature. They are the primary readability feature. The point of typed documents and explicit flow/task contracts is not academic purity; it is to let a reader understand the system without navigating the entire codebase.

This is especially important in AI pipelines, where the real cost of a bad abstraction is not just bugs but cognitive overload. The framework should make it natural to write code that explains itself.

---

## 5. Documents Are Semantic Artifacts, Not Transport Hacks

A document should exist because it represents a meaningful artifact in the domain, not because the code needs a place to stash temporary state or shuttle unrelated fields between steps.

The purpose of document types is to make provenance and meaning explicit. When a task consumes or produces a document, a reader should understand what kind of artifact it is without reading the task that created it. Documents are the language of the pipeline.

This means:
- documents should represent domain-relevant artifacts
- document types should communicate origin and meaning
- document proliferation is not good in itself
- a large number of documents is only justified if each one materially improves understanding and traceability

The opposite pattern is creating fake or throwaway documents purely to satisfy an API or move temporary values around. That is not document-centric design; it is type abuse.

**Good**
A document type represents something a human debugger would care about reading in isolation.

**Bad**
A document exists only because a task needed to carry a few IDs, references, flags, or prompt fragments to the next step.

---

## 6. Flows Orchestrate, Tasks Work, Deployments Plan

Each layer has one job.

### Deployments
Deployments define the pipeline plan: what flows exist, in what order they may run, and under what conditions some steps are skipped. They own the large-scale execution shape.

### Flows
Flows orchestrate tasks. A flow should be a readable sequence of task calls and small, obvious control decisions. Flows should not perform hidden work, absorb task responsibilities, or turn into pseudo-runtime engines.

### Tasks
Tasks do atomic work. A task should represent one coherent business operation and be individually runnable and testable. It should not orchestrate other tasks or hide mini-workflows internally.

These layer boundaries matter because they preserve debugability. If tasks start orchestrating, or flows start becoming giant execution engines, the code stops reflecting the actual runtime structure. The framework is built to trace and explain a hierarchy; application code must respect that hierarchy.

---

## 7. The Framework Should Encourage Small, Straight-Line Code

Readable pipeline code is boring in the best sense. It is direct. It is not clever. It does not compress multiple concerns into one construct. It does not hide orchestration inside helpers that make control flow harder to see.

The preferred shape is:
- deployments that read like a plan
- flows that read like orchestration
- tasks that read like one unit of work
- data transformations isolated into pure, obvious helpers when needed

If a flow becomes unreadable, the answer is usually not “invent a more clever flow.” It is:
- split responsibilities more cleanly
- move one-off behavior to an earlier or later step
- reduce the number of concepts the flow must juggle
- remove document plumbing from the visible code path

The framework should support authoring styles that naturally keep code short and obvious. It should not require clever patterns to stay within a quality bar.

---

## 8. Python Is the Orchestration Language, Not a Mini-DSL

The framework should work with Python, not against it. Sequencing, small conditionals, loops, and clear task calls are legitimate and often the best way to express orchestration.

A common failure mode in workflow systems is building a second language on top of Python. This usually begins as an attempt to make things “structured” and ends as a pile of abstractions that are harder to read than plain code.

The framework should only introduce higher-level orchestration constructs when they remove real, repeated boilerplate without making the resulting code less obvious. If a new abstraction makes a simple flow harder to understand or turns domain logic into framework ceremony, it is the wrong abstraction.

This is why many “block graph” or “declarative DSL” ideas are appealing in theory but harmful in practice. They compress visible logic into hidden machinery. For this framework, plain readable Python remains the preferred medium for orchestration.

---

## 9. Determinism and Precomputability Matter

The framework should favor designs that let the execution shape be understood ahead of time. This is not only about progress reporting; it is about debugging, reasoning, review, and preventing hidden complexity from creeping into application code.

When a pipeline can be rendered, inspected, and reasoned about before execution, teams can detect:
- hidden fan-out
- unbounded loops
- surprising task calls
- implicit branch explosions
- dead outputs
- missing consumers

The framework does not need to know the exact runtime outcome in advance, but it should make the possible shape of execution predictable and inspectable. A flow should not be able to hide arbitrary orchestration tricks inside dynamic Python patterns that defeat static understanding.

This principle exists to support correctness and legibility in large systems. If the structure cannot be understood before execution, it will eventually become too complex to maintain.

---

## 10. No Silent Data Loss

The framework must never normalize failure into silent truncation. If a task or flow produces more data than expected, the system should surface that fact clearly. It may warn, it may ask the producing step to compress or regroup its output, but it must not silently discard information.

This principle applies broadly:
- no slicing outputs to force them under a cap
- no quiet dropping of items from collections
- no implicit shrinking of fan-out to satisfy a bound
- no “best effort” suppression of artifacts that were actually produced

Data loss disguised as convenience is one of the worst categories of framework behavior because it creates pipelines that appear to work while silently becoming less correct.

---

## 11. Full Traceability Is a Feature, Not an Implementation Detail

The framework’s promise includes full traceability: what happened, in what order, using which artifacts, with what lineage. That traceability must be automatic and reliable.

However, this does **not** mean application authors should think in terms of storage or logging. The framework owns traceability so users do not have to. What matters to the author is that the system can later explain:
- where something came from
- what step produced it
- what later consumed it
- how state evolved over time

Traceability is a first-class user-facing capability, even though the mechanics behind it should stay hidden. The framework should preserve a rich execution record without forcing developers to write “for tracing” code.

---

## 12. Testability Shapes the Design

A correct authoring model is one where every task and flow can be understood and exercised on its own. The framework must reward decomposition into individually runnable, serializable, strongly typed units.

This is one reason immutable, serializable boundary values matter so much. If inputs and outputs are clean and explicit, isolated testing becomes straightforward. If tasks depend on ambient state or mutate shared structures, correctness becomes much harder to establish.

Testability is not only about test suites. It is a design pressure that forces honest APIs. If something cannot be easily run and reasoned about in isolation, it is usually not well designed.

---

## 13. Readability Beats Cleverness

The framework should reward code that is obvious to a senior engineer reading it for the first time. Dense comprehensions, overloaded helper functions, overloaded return shapes, hidden orchestration, type-erasing shortcuts, and clever dispatch tricks all reduce maintainability, even when technically valid.

Readable code has these traits:
- one concept per line
- meaningful intermediate names
- stable, unsurprising patterns
- no hidden control flow
- no “look elsewhere to understand this line”

This matters even more for AI-generated code. AI agents will happily compress meaning into short but unreadable constructs if the framework makes that easy. The framework should instead guide them toward shapes that remain obvious after generation.

---

## 14. Clean Correctness Is More Important Than Legacy Compatibility

When a design is wrong, the right answer is to replace it with a cleaner, more correct model rather than preserve it indefinitely for compatibility’s sake. A framework that prioritizes legacy affordances over architectural integrity eventually teaches bad habits.

Backward compatibility is valuable, but it is not the highest value. The highest values are:
- correctness
- readability
- predictability
- strong contracts
- user experience
- mistake prevention

When those are in tension with preserving older, weaker patterns, the framework should prefer the clean model.

---

## 15. The End User Experience Should Feel Obvious

The final test of the doctrine is simple: a developer building on the framework should feel that the system makes good structure natural.

They should feel:
- “I write business logic in Python.”
- “The framework takes care of the rest.”
- “The code tells the truth about what it needs and what it does.”
- “The obvious thing is the correct thing.”
- “I do not need tricks to express a clean pipeline.”

They should **not** feel:
- “I need to know the storage internals.”
- “I have to remember hidden conventions.”
- “I need hacks to fit the framework.”
- “I am constantly assembling and filtering arbitrary bags of artifacts.”
- “I can only make this work by abusing the type system.”

That is the real standard. If the framework can be used correctly only by developers who already know all of its internal traps, then the framework is still too hard to use.

---

## Criteria for Judging Any New Pattern

When evaluating a new framework feature or application pattern, ask:

1. **Does it make the wrong thing impossible, or merely detectable?**
2. **Does it reduce visible business-logic noise, or just move it somewhere else?**
3. **Does it hide mechanics while keeping meaning explicit?**
4. **Can a reader understand the code locally?**
5. **Does it preserve strong contracts?**
6. **Does it keep application code focused on business logic?**
7. **Does it improve traceability without exposing operational details?**
8. **Would an AI agent naturally use it correctly?**
9. **Does it eliminate an entire class of bad code, or only react after the bad code is written?**
10. **Does it make pipelines more predictable, inspectable, and debuggable before execution?**

If the answer to most of these is “no,” the pattern does not belong in the framework.

---

## Final Doctrine

ai-pipeline-core should be a framework where the application author writes clear business logic and the framework makes that code:
- structurally sound
- easy to debug
- easy to review
- easy to test
- easy to trace
- hard to misuse

Its success should not be measured by how many features it exposes. It should be measured by how little bad code an application author can write while still remaining productive.
