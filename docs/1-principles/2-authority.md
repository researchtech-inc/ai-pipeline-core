# Authority

## Current doctrine governs raw history and current code

Current accepted doctrine governs future work. Task transcripts, old documentation, prior agent output, `.tasks/`
material, current implementation behavior, and pre-0.3.0 code are evidence. They are not authority over current
intent. This matters because agents copy visible code and detailed history as precedent; if current artifacts are
allowed to define intent, implementation accidents become the next author's specification.

The 0.3.0 contract is written as the target the implementation is built to satisfy. When current code differs from
that contract, the difference is evidence of implementation work remaining, not a reason to weaken the contract
locally. When raw history exposes a real gap in current doctrine, surface the gap and repair the owning doctrine
surface; do not apply the historical material as a parallel rule.

## Drafts do not govern adjacent work

Agents may draft changes to principles, problem frame, contracts, design, implementation doctrine, public surfaces,
runtime rules, prompts, tools, or code. A draft is not accepted authority merely because it exists, appears correct,
or sits beside dependent work.

Do not judge implementation, tests, examples, or adjacent doctrine against a draft rule or contract before human
approval makes that draft current. Bundled rule changes and dependent work are allowed only when the rule change is
separated for review and the dependent work is explicitly conditional on approval. Otherwise an agent can move the
target and then declare its own work compliant.

## Contracts are build inputs; code conforms to the contract

Consumer-facing behavior is a promise before it is an implementation detail. Design, implementation, tests, tools,
runtime surfaces, and examples build from accepted contracts; they do not derive the contract from what the code
happens to do.

If work depends on a behavior another author, operator, external system, persisted record, or downstream workflow may
rely on, verify the accepted contract that owns it before building against it. If the contract is missing,
incomplete, or wrong, propose the contract repair first and keep dependent lower work conditional on that approval.

Tests and examples verify or illustrate accepted promises. They do not create promises by existing.

## Later work satisfies earlier commitments

The 0.3.0 contract is the build input for this migration and for future applications. If lower work quietly weakens
an earlier commitment, the spec remains present but stops constraining what ships; the project gets the cost of
doctrine without the safety of doctrine.

The satisfaction direction is the mechanism that prevents that hollowing-out. Implementation satisfies design;
design satisfies contract; contract satisfies the problem frame; the problem frame respects these principles.
Discovery may begin anywhere, but discovery does not move authority.

When lower work cannot satisfy an earlier commitment, stop treating the conflict as a local detail. Either satisfy
the earlier commitment or propose a separated change to the owning layer and keep dependent work conditional on
approval. A lower-layer workaround that quietly weakens an earlier promise is a defect even if the local artifact
works.

## Repairs land at the owning cause

A failure surfaces where it is observed, not necessarily where it is caused. A broken test can reveal wrong
implementation; repeated implementation strain can reveal wrong design; design strain can reveal an overbroad
contract; recurring agent misuse can reveal a defective framework surface.

Repair the owner of the cause, then align dependent surfaces. Changing an assertion, prompt, expected output, local
branch, wrapper, or warning while the owning cause remains active is suppression, not repair. Recurring symptoms are
evidence that the cause sits at a shared or earlier surface.

If the owning cause is outside recorded task scope, separate it, escalate it, or explicitly expand scope before
repairing it.

## One question has one authoritative answer

One question has one owning answer. The same subject may appear in multiple places only when each place answers a
different question: outward promise, internal mechanism, authoring rule, operator behavior, runtime interop shape,
or implementation procedure.

Do not answer one question in two API surfaces, tools, runtime shapes, configuration paths, or other public
framework surfaces. Do not preserve duplicate answers by calling one primary and one secondary. If either surface
can replace the other as the full answer, one of them is wrong and the owner must be repaired.

This applies to code and public framework surfaces, not only prose. A runner surface, CLI command, runtime endpoint,
schema, and document model may each expose a local consequence, but only one surface owns the full answer to the
question it answers.

A hand-maintained mirror of a typed owner is the same defect in code; its coding-baseline form is
`7-coding-baselines.md` under `No unvalidatable derivatives`.

## Verify against the record, not recall

For completed or in-flight work, the durable record outranks memory, the last observed event, elapsed time, a log
tail, or a prior agent's summary. A run's state, outputs, documents, cost, spans, provenance, and logs are known
through the record and the supported read surfaces.

Diagnose, recover, compare, and benchmark from recorded artifacts. A claim about what a run did must be checked
against the run record; a claim about a document must be checked against its identity, content, and provenance; a
claim about cost or cache behavior must be checked against recorded cost and unit status. When the record is absent,
the work is not available as evidence for later readers.
