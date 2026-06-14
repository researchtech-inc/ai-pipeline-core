# Non-Goals

These are tempting directions the project explicitly rejects. Most drift arrives as one of these wearing new
words, which is why they are named here explicitly. Each entry names what is rejected, why it is tempting,
why it is rejected, and what remains allowed.

Behavioral rejections aimed at authors — compatibility scaffolding, silent scope expansion, suppressing
failures instead of repairing causes — are owned by the principles layer and are not restated here. This file
rejects directions for the framework itself.

## Not a generic workflow engine

The framework's machinery — orchestration, persistence, failure handling — could plausibly serve any workflow,
and generalizing it looks like leverage. Rejected: generality multiplies the number of valid shapes for the
same intent, and this frame's central bet is that shape divergence under machine-speed authorship is the
enemy. A generic engine also abandons the fit boundary that justifies the strictness; it would impose
analytical-grade discipline on work that does not need it and be rightly refused.

Still allowed: non-analytical steps inside an analytical application, and other projects consuming the
framework as a substrate for their own purposes.

## Not a chat wrapper or assistant platform

Conversational assistants are the most visible AI products, and the framework's components look reusable for
them. Rejected: the fit boundary. Latency-sensitive, session-shaped, single-call products pay all of the
framework's strictness and collect none of its returns, and optimizing the surface for them would pull its
defaults away from the long-chain evidence-bearing work the framework exists for.

Still allowed: conversational interfaces in front of pipeline outputs, built outside the framework. This
rejection is not aimed at the in-scope surface for launching, observing, and reading runs — operating an
analytical run from outside is not a conversational, session-shaped product, and the two should not be
conflated.

## Not solving reliability with stronger models

Every model generation visibly improves judgment, so "the next model will fix it" is always the cheapest plan
on paper. Rejected: model improvement is roughly linear and compounding is exponential — the arithmetic in
3-hard-parts.md is unforgiving at production length, and a plan that requires near-perfect steps is not a
plan.

Still allowed — required, even: adopting better models eagerly. The frame assumes capability keeps improving;
what is rejected is treating that improvement as the structural answer.

## Not chasing repeatable answers by constraining the interpreter

Identical reruns look like reliability, and lowering temperature, pinning outputs, or retrying until answers
match produces them cheaply. Rejected: the divergence such tuning suppresses is the defect signal of an
underspecified judgment — a reality 3-hard-parts.md owns. Constrained randomness hardens one accidental
interpretation into apparent truth and hides exactly the ambiguity that should surface as a defect in the
question being asked.

Still allowed: making mechanical behavior reproducible, where mechanics are genuinely the question. The
rejection is interpreter-tuning as the fix for ambiguous definitions.

## Not hiding ambiguity behind polished output

Consumers want one answer, and unresolved states read as unfinished work. Rejected: confidently wrong outputs
are this domain's most expensive failure class. Resolving ambiguity before evidence warrants it contaminates
every dependent judgment, and a premature merge costs far more to undo than a late one. Polish that conceals
an unresolved state converts a visible limitation into an invisible error.

Still allowed: explicit resolution when evidence warrants it. The rejection is silently resolving — or
silently omitting — what the evidence has not settled.

## Not treating inspectability as a substitute for correctness

Structural properties are measurable and analytical quality is not, so the pull toward optimizing traceability
and validation as if they were quality is constant. Rejected: a fully traceable wrong answer is still wrong,
and presenting structural integrity as evidence of analytical quality relaxes review at the precise point
where it must not relax.

Still allowed: valuing structure for what it actually buys — diagnosis, repair, audit. The rejection is
offering it as proof that the analysis is right.

## Not absorbing domain methodology

Every application needs evaluation rubrics, source-reliability policy, and scoring rules, and the framework
sees all of them — shipping them looks like saving every application the same work. Rejected: domain
epistemics differ in exactly the places that matter, absorbing one domain's rules would narrow the class-wide
frame to a single product, and the framework would begin competing with its own applications. The bright line
holds: generic structure is the framework's; domain judgment is the application's.

Still allowed: applications expressing their own methodologies on the framework's substrate, and adjacent
projects packaging methodology as products that consume the framework.

## Not becoming the stateful platform built on top of it

The framework records every run, so the systems one step up — persistent cross-run workspaces, invalidation
cascades, probabilistic graph engines, scoring and verification queues, living assessments that learn across
engagements — look like natural extensions: the data is already there. Rejected: those are products that
consume the framework, not capabilities of it. Absorbing them collapses the boundary between substrate and
consumer, couples every application to one downstream product's epistemics, and grows the framework past what
its own authors can hold.

The line is drawn at the run. Launching, observing, recovering, and reading back a single run — and standing
up the machinery that executes it — are in scope, because every application needs them and none should rebuild
them. What stays out is everything that accumulates, scores, or reasons over state across many runs: that is
where one downstream product's epistemics begin, and it belongs to the products and the separate methodology
projects that consume the framework, not to the framework.

Still allowed: the substrate capabilities such systems genuinely need — durable records, stable identity,
recoverable history — offered as framework guarantees for any consumer to build on.

## Not a user interface or dashboard product

Once a run can be launched, observed, and read from outside, the surface that exposes that data looks one
short step from the screen that renders it, and someone will ask the framework to draw the dashboard. Rejected:
the framework owns the data, the observable account of a run, and the surface that carries them; it stops
there. Rendering, presentation, and the human-facing application are a different product with a different
audience, and bending the framework toward pixels pulls its surface away from the systems that consume it
programmatically.

Still allowed — expected, even: a user interface, dashboard, or frontend built outside the framework against
its observation and read surface.

## Not an analytics or reporting warehouse over the run record

A rich surface for reading what a run did invites the next request: aggregate it, query across all runs,
report on it, turn the record into a business-intelligence store. Rejected: the record and its read surface
exist to operate one run and to trace one conclusion back to its evidence, not to answer arbitrary cross-run
analytical questions. Growing the framework into a warehouse couples it to reporting needs that change far
faster than the substrate and that differ from one consumer to the next. Comparing many recorded runs to decide
how to improve the system — establishing a baseline and then measuring where a cheaper way of making a judgment
is good enough and where it is not — is exactly such a cross-run question: the framework preserves each run's
inputs and outputs so a consumer can ask it, and re-executes a unit so a consumer can measure it, but performing
the comparison, scoring the results, and deciding from them belong to the consumer. The record is the substrate
for that measurement; it is not the engine that performs it.

Still allowed — expected, even: an external system or agent reading the record to measure, compare, and weigh
many runs against each other, and to build its own analytics, aggregation, or reporting on top of the
framework's guarantees.

## Not the authentication, authorization, or trust-boundary product

Bringing the surface that launches and observes runs into the framework makes it look as though the framework
should also decide who may call it, how it is exposed, and where one tenant ends and the next begins. Rejected:
who is allowed to operate a run, how the surface is exposed to outside callers, and the multi-tenant trust
boundary stay at the platform edge. A framework that absorbed them would have to model an identity, exposure,
and tenancy world that belongs to whoever operates it, and would tie every deployment to one answer.

Still allowed: an operator placing the framework's surface behind their own authentication, authorization, and
exposure layer.

## Not a divergent local or development mode

A local way to run that is lighter than production — skipping the recording, the observation, or the durable
record to start faster — is easy to build and feels fast. Rejected: a local path that behaves differently from
production means local success no longer predicts production behavior, the loop agents iterate in is not the loop
that ships, and the gap surfaces at the most expensive moment to find it. A cheap local path that drops the
record also cannot be inspected, re-measured, or studied the way a production run can — and that record is the
substrate the whole iteration loop depends on.

Still allowed — required, even: local execution that is the same loop with the surrounding machinery swapped
beneath it — the same operations, the same record, the same observation, differing only in scale.

## Not making each application stand up and operate its own machinery

Exposing the runtime as low-level parts and letting each application assemble its own way to stand up, launch,
run, study, and ship is less design work than drawing one coherent operating surface, and sophisticated adopters
ask for it. Rejected: that recreates the same generic operating burden in every application and every team, and
fragments the operating surface the way unstructured authoring fragments code — at machine speed, whatever way of
operating is visible becomes the one the next agent copies. A loose collection of unrelated operator entrypoints,
or per-application launch and run wrappers, is the operating-side version of the divergence this frame exists to
prevent. The rejection runs in the other direction too: the framework itself must not answer one operating
concern with many parallel setup and operation pathways that each team has to choose among and wire together —
that is the same burden and the same divergence, moved from the application into the framework's own surface.

Still allowed: distinct capabilities under one coherent operating surface, and an advanced programmatic surface
for specialized in-process use.

## Not a broadly configurable or pluggable framework

Offering a menu of interchangeable ways to satisfy the same generic concern — several swappable mechanisms for
one operational role, each adopter picking their own — reads as power and reach, and letting the adopter choose
looks like it widens who the framework serves and avoids ever betting on the wrong mechanism. Rejected on three
forces already in this frame. Every interchangeable option is another visible shape the next agent copies and
another axis along which authored and operated systems diverge — the same machine-speed convergence on whatever
is visible that this frame treats as the enemy. Supporting many alternatives for one concern multiplies the
machinery the framework must keep correct, working directly against the small-surface and operational-burden
forces. And a single chosen way can be made fast and turnkey to stand up where a menu cannot, so committing to
one way is part of how standing-up and launching stay fast and off the author. The framework commits to one
deliberately-chosen supported way for each such concern, not a set of alternatives.

Still allowed: an application or adjacent project building its own alternative mechanism outside the framework
when its needs genuinely differ; and the narrow case where several alternatives occupy one role that the author
and operator already meet as a single shape, so admitting them adds no new shape for the next agent to copy and
no new operating burden — as with external model providers, which differ from one another yet fill one such role
in the chain, so letting a judgment use whichever serves it best widens no surface the author or operator works
against and still helps real cases. Why such a role can hold several alternatives without widening that surface,
and which single way is chosen for every other concern, are later-layer matters; the exception here is narrow and
force-justified, not an opening of general configurability.

## Not buying speed by weakening the record

Dropping recording, validation, or persistence makes a run start and finish faster, and under iteration pressure
that looks like the cheapest speedup available. Rejected: the record is what makes machine-speed authorship safe
to operate and is the only substrate for studying, re-measuring, and recovering work — the very things the fast
loop exists to enable. Speed must come from reusing already-done work and from not rebuilding what has not
changed, never from skipping what a run leaves behind. A run made faster by recording less has not gotten
cheaper; it has moved the cost into every later attempt to understand or improve it.

Still allowed: reusing prior results so unchanged work is not repeated, and avoiding avoidable rebuilds and
startup overhead.

## Not requiring authors to learn the machinery for ordinary use

Exposing internals is less design work than drawing a deliberate surface, and sophisticated users genuinely
ask for it. Rejected: authors — agents above all — entangle with whatever they can reach, and entangled
machinery freezes: it stops being changeable the moment ordinary applications depend on its details. An
operating model that prices internals knowledge into ordinary authorship pays twice — in every author's
ramp-up and in every internal change thereafter. The same holds for operating an application, not only authoring
one: running, observing, re-running, and shipping a system in the ordinary case must not require learning the
internal machinery or assembling glue around it, or operators entangle with internals exactly as authors would.

Still allowed: rich machinery itself, and deep inspection of it when diagnosing failures. The rejection is
internals knowledge as the price of ordinary authorship.

## Not optimizing the authoring surface for human developers

Human developer conventions — flexibility, optional shortcuts, clever brevity — are the familiar default, and
sophisticated users genuinely ask for them. Rejected: the authors are agents, and for them flexibility is
permission to diverge — every additional valid shape for the same intent is another shape the next author will
copy. Ergonomics tuned for a patient, judgment-exercising human optimize for an author this class of system
does not have.

Still allowed — required, even: human readability of everything the system produces and contains, because
humans review it. The rejection is trading the single correct authoring shape for human convenience.

## Not correcting the authors instead of the framework

When an agent misuses the framework, telling the agent looks like the fix: an instruction takes a minute and a
framework change takes a day. Rejected: the authors do not persist. An instruction evaporates with the
session, the next author makes the same mistake, and the accumulating rulebook decays as it grows — the
correction lands on the one party that cannot durably receive it, while the surface that invited the misuse
keeps inviting it. Recurring misuse is evidence about the framework, and the framework is where a correction
compounds.

Still allowed: task-local direction within a single session, and temporary advisories while the structural
correction is being prepared. The rejection is instruction as the durable correction channel.

## Not making humans the recovery path

When an agent-built system fails, a human stepping in — hand-editing the code, manually reconstructing the
run — is always locally fastest, and each instance looks like pragmatism. Rejected: a design that assumes a
human will repair what agents produce reintroduces the bottleneck this class of system exists to pass, caps
every loop at human speed, and converts the framework's gaps into a standing human duty that appears on no
backlog.

Still allowed: humans reviewing conclusions, documents, and diffs, and approving changes. The rejection is
human hands inside the loop — authoring, repairing, or routinely diagnosing in the agents' place.

## Not accepting manual forensics as the operating mode

At small scale, an engineer rereading a run answers any question, so building for diagnosability feels like
overhead and the capability to inspect, re-execute, and measure past work reads as a sidecar to ship later.
Rejected: hours-per-issue reconstruction does not survive production volume; a system its authors cannot
inspect, re-execute, and measure is not maintainable by them at all; and a team that normalizes
reconstruction quietly converts "we can find out" into "we trust it" — which is the accountability promise of
the mission dying in place.

Still allowed: deep manual investigation of novel failures. The rejection is manual reconstruction as the
routine answer to routine challenges.
