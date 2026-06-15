# Agent Work and Review

## Human review is the final correctness gate

AI agents author application code, framework changes, and review evidence at machine speed. Human review remains the
final correctness gate for changes to accepted doctrine, contracts, public surfaces, runtime rules, and repository
artifacts.

Agent review, passing tests, successful verification, clean structured output, and multi-agent agreement are evidence
for the human decision. They do not become the decision. A workflow that lets agent consensus, CI success, a
"routine" label, or silence stand in for explicit human approval removes the accountability boundary this project
depends on.

## Multi-agent agreement is evidence, not proof

Agreement among agents is useful only for what those agents actually checked. Agents can share omitted context,
training priors, misleading visible defaults, and the same incomplete interpretation of a contract. Same-family
agreement is especially vulnerable to correlated blind spots.

Treat agreement as review evidence, not correctness. Preserve grounded dissent and material uncertainty. A concern
does not become correct because more agents agree with it, and it does not become irrelevant because fewer agents
raised it. The question is what the review evidence proves, what it leaves unresolved, and what the human reviewer
must decide.

## Cited objections must prove applicability

A cited objection is grounded only when the cited commitment exists, is current, applies to the reviewed surface, and
is actually violated by the work. A path, heading, official tone, old task record, similar topic, or confident agent
claim does not make an objection binding.

If the objection is grounded, answer it before the work is treated as clean: repair the work, revise the owning
surface, escalate the conflict, or have the human reviewer explicitly accept the known risk. If the citation fails
existence, currency, scope, or actual-violation checks, record why it does not block as cited and preserve any
remaining material concern through the uncertainty path below.

## Material uncertainty escalates

Review does not produce only approval or grounded rejection. A reviewer may lack enough context, find a material
risk that current doctrine does not yet name, or see a failed citation whose underlying concern still matters.

Material uncertainty is neither approval nor veto. Record the concern, the surface it may affect, the condition that
could change the decision, and what evidence, human decision, follow-up, or owning-surface repair is needed. Do not
hide uncertainty to present clean consensus, and do not inflate unsupported concern into a binding doctrine
violation.

## Reject invalid options before ranking valid ones

Compliance with accepted commitments is a precondition for consideration. An option that violates a principle,
problem-frame commitment, non-goal, accepted contract, governing design, runtime rule, recorded task scope, or
explicit maintainer constraint is unavailable until the owning commitment changes.

Do not rank invalid options against valid ones by speed, simplicity, cost, or elegance. That turns prohibition into
a tradeoff. If no valid option remains, escalate or propose a change to the owning commitment and keep dependent work
conditional on approval.

## Stay inside recorded task scope

Scope is the unit that makes context selection, review, and approval meaningful. Agents will see nearby cleanup,
better names, refactors, missing tests, and future improvements while working. Those changes are not in scope merely
because they are good.

Do the work needed to satisfy the recorded task and its necessary consequences. If adjacent work is required for the
task to be correct, record why and escalate when it materially broadens affected surfaces. If it is merely useful,
separate it or ask for explicit scope expansion before doing it.

Silent scope expansion is especially harmful in this repository because it mixes doctrine, contract, code, runtime,
and tool decisions in one review packet. Keep the decision boundary visible.

## Refinement requires observed need

A refinement earns its cost only when it answers an observed signal: a failing case, an accepted contract clause, a
recorded review confusion, a measurement, a security finding, a problem-frame force, or another accepted doctrine
requirement.

Do not add speculative edge handling, knobs, adapters, indirection, generalized abstractions, optional pathways,
defensive branches for impossible states, or future-proofing because they might help later. Each new branch becomes
another shape agents can copy and reviewers must understand. Add it when the need is real, current, and owned by this
surface.

## Rules stay absolute within their stated scope

A rule that depends on each agent deciding when to ignore it is not a rule strong enough for machine-speed
authorship. State rules in the narrowest scope where they can be followed as written, then keep them absolute inside
that scope.

Real exceptions take one of two paths. A one-time human-approved deviation stays task-local and recorded as such. A
durable exception revises, narrows, splits, relocates, or removes the owning rule through the normal approval path.
Do not keep side permissions, warnings, or local prompt instructions beside a rule they weaken.
