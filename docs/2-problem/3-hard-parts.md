# Hard Parts

These are recurring realities of building production analytical AI systems. They are not defects to eliminate
and not workstreams to schedule; they are conditions any honest solution must design around. Designs that
pretend a hard part is absent fail at exactly the scale where failure is most expensive.

## Errors compound across long analytical chains

### Reality

Serious analytical work is not ten steps. A production investigation chains hundreds of dependency-bearing
judgments: planning, gathering, source assessment, cross-checking, conflict handling, consolidation,
synthesis, review. Each imperfect judgment inherits prior loss and adds its own, so reliability degrades
multiplicatively: at ninety-nine percent per-step accuracy, ten dependent steps still look excellent at
roughly ninety percent, one hundred steps fall to roughly thirty-six percent, and five hundred steps land
below one percent. The figures are illustrative — checkpoints, uneven dependency, and varying failure rates
move them — but the direction never changes.

Worse than the rate is the qualitative shift. At demo scale errors are random and obviously wrong: a broken
citation, an impossible date. At production scale they become systematic and plausible — confidently stated,
well-sourced conclusions built on a flawed intermediate judgment, internally consistent because everything
downstream is coherent under the inherited error. And the asymmetry is upstream: a defect in an early judgment
propagates through every dependent surface, so equivalent defects cost more the earlier they occur.

### Why naive approaches fail

Stronger models, retries, prompt tuning, and lowering temperature raise per-step accuracy and leave the
multiplicative dynamic untouched. Model improvement is roughly linear; compounding is exponential — moving a
step from ninety-nine to ninety-nine-and-a-half percent moves a five-hundred-step chain from under one percent
to about eight. These responses also deepen the trap: they make short chains look even better, postponing the
discovery that the shape fails at length.

### What later layers must remember

End-to-end reliability cannot be inferred from local accuracy, and short-run success is not evidence about
production behavior. This is a property of chain depth that any solution must contend with; it is not answered
by making any one step smarter.

### Not prescribed here

Chain-length limits, verification schedules, checkpoint placement, review formats.

## The execution substrate is unreliable, volatile, and metered

### Reality

These systems run on external model providers and services that fail mid-flight in many distinct ways, behave
differently from one another and from their own past behavior, and charge for every interaction. At
production volume the arithmetic is unforgiving: across hundreds to thousands of calls some always fail, one
upstream incident correlates failures across everything in flight, and behavior that held at hour one can
shift beneath a running system with no application change at all. The meter compounds the exposure: this work
spends real money at machine speed, so a defect that loops or fans out without bound is a financial incident
as well as a quality one. The compounding of error across a chain is about judgment quality degrading through
dependent steps; this hard part is about the ground those steps execute on.

### Why naive approaches fail

Assuming calls succeed builds a system that cannot finish a production run. Retrying blindly amplifies load
at exactly the moment the substrate is degraded. Hand-rolling handling in each application re-solves the same
hostile environment inconsistently in every codebase. And a clean demo run is evidence about a calm hour, not
about the substrate.

### What later layers must remember

Per-call unreliability, behavioral drift, and per-call cost are properties of the environment, not defects to
fix once. Any solution must stay operable while the substrate misbehaves, and must keep machine-speed
spending bounded and attributable. Observing and controlling a run ride the same distributed, unreliable
substrate as the work itself and inherit the same failure modes, so the surfaces that watch and control a run
cannot assume a reliability the execution never had. Standing a run up and starting it ride that substrate too,
so the path from "launch" to "running" inherits cold starts, scheduling delay, and partial failure and is never
instantaneous — a solution that keeps the loop fast must do so by removing avoidable overhead, not by pretending
the ground beneath it is frictionless.

### Not prescribed here

Retry policy, routing, caching, budget mechanisms, failure taxonomies.

## Failures surface far from their causes

### Reality

A visible failure appears far below or long after the judgment that introduced it. As dependent work grows,
the observation site and the origin site separate — across steps, across phases, across levels of abstraction
— and the dependency graph exceeds what any reader can hold in working memory. The symptom site is the only
thing in view, and it lies: a final contradiction may originate in an early source-quality call, an unresolved
identity, or a comparison that dropped the material that would have changed the answer. The distance is
temporal and economic as well as structural: runs are long and expensive, so a defect discovered hours in
invalidates everything spent downstream of it, while the same defect surfaced before work began would have
cost almost nothing.

### Why naive approaches fail

Fixing where the failure surfaced — adjusting the assertion, patching the output, adding a special case —
closes the symptom and leaves the cause active, so the next case breaks somewhere else. Spot-checking final
outputs fails for the same reason compounding makes errors dangerous: the failures that matter are precisely
the ones that look right.

### What later layers must remember

When the path from a conclusion back through its intermediate judgments cannot be reconstructed, a plausible
wrong answer is indistinguishable from a right one, and every defect becomes a manual forensic exercise whose
cost scales with the system instead of the bug. And the later a structural defect is allowed to surface, the
more spent work it takes with it.

### Not prescribed here

Lineage representations, diagnosis tooling, repair routing.

## A failed run cannot be studied by running it again

### Reality

Runs are long, expensive, and partially non-deterministic. A defect observed deep in a run cannot be
reproduced on demand: the rerun is a different run, costs real money and hours, and the behavior under study
may simply not recur. The same fact governs improvement: whether a changed judgment, instruction, or model
choice helped cannot be derived by reading it — it can only be measured against recorded behavior, because
the behavior is empirical, not derivable. So whatever a run leaves behind is the only substrate diagnosis and
verification have — and that record is itself oversized: a production run produces more calls, intermediate
results, retries, and failures than any reader can consume raw, and the reader doing the diagnosing is an
agent with a bounded working context. The surfacing of failures far from their causes is about the distance
between a symptom and its cause; this hard part is about the ground a diagnosis has to stand on at all.

### Why naive approaches fail

Rerun-and-hope spends a run's cost to produce a different run. Log archaeology buries the load-bearing detail
in volume. Exact-match assertions on sampled output fail honestly and pass dishonestly, and substituting a
scripted stand-in for the judgment verifies the stand-in, not the system. Pronouncing a change an improvement
by reading it mistakes plausibility for measurement.

### What later layers must remember

Recorded execution is the only debugging and verification substrate there is. It must be consumable in
bounded portions without losing what matters, and diagnosis and experiment must be cheap relative to a full
run — or they will not happen. The same record is also read while the run is still in flight, and by systems
other than the one that produced it, so "consumable in bounded portions" is a requirement for live and
external readers too, not only for after-the-fact diagnosis by the producer. And the substrate is not only the
single run under study: the accumulated record of many past runs — including those already serving production —
is what broader questions are settled against, such as where a cheaper way of making a judgment is good enough
and where it is not. That works only if every run preserved enough of what it did — its inputs and its outputs,
not merely that it ran — to be re-measured and compared later without re-running the original work. What stays on
the framework's side of the line is the substrate: the durable inputs and outputs, the ability to re-execute a
unit, the cost attributed to it. The methodology that compares runs, and the judgment of which result is better,
belong to the consumer reading the record, not to the framework. And that substrate is not emitted by the
application's steps alone: it is produced by every part that touched the run — the machine, the executing and
scheduling machinery, the operating surface, the application's own steps, and the external services the run
called — each keeping its own account. Scattered across separate places reached separately, those accounts are
useless to a reader with bounded working context; the substrate becomes one an agent can work from only when all
of it is gathered together and tied to the run.

### Not prescribed here

Diagnostic record formats, re-execution mechanics, test harnesses, comparison tooling.

## Iteration speed decides whether a long run is ever improved

### Reality

A production run takes hours and spends real money, and improving one is empirical: change a judgment, an
instruction, or a model choice, run it again, and compare. The cost of one such cycle is the time and money
between making a change and observing its effect — and at production scale that cost is a full re-execution plus
whatever it takes to stand the run up and start it. If every change pays that cost, learning anything is
prohibitive and the system simply stops being improved. The latency is not paid once; it is paid on every
correction, and it compounds across the many corrections one application needs and across a portfolio of
applications the way error compounds across a chain.

### Why naive approaches fail

"Just run it again" makes every experiment cost a whole run. Faster models do not change the arithmetic — the
cost is the chain, not the step. Reassembling the environment for each run adds a fixed tax to every cycle. And
"keep everything and reuse it" silently reuses stale work unless what changed and what did not are
distinguishable, which is a structural property the solution must provide rather than assume.

### What later layers must remember

The cost of observing the effect of a change must be proportional to the change, not to the size of the run: the
unchanged majority of a long, expensive run must be reusable, and the changed unit must be exercisable in
isolation with the same recording a full run produces. This is distinct from the hard part above — that one is
about the non-reproducibility of a single run; this one is about the economics of the improvement loop, where the
binding cost is paid on every correction.

### Not prescribed here

Reuse, resume, partial-execution, or single-unit mechanics; how a run is stood up or started; how cold starts are
handled.

## Live observation is lossy; the durable record is complete but slower

### Reality

These runs last hours, and they are watched while they run — by operators, by the systems that launched them,
and by the agents diagnosing them. There are two ways to watch, and they pull against each other. A live
signal is immediate but lossy: whoever is watching connects late, drops, and reconnects, and whatever passed
while they were gone is gone. Reconstructing from the durable record is complete but slower and heavier, and
on its own it cannot show what is happening right now. Worse, the most recent thing a watcher saw is not the
same as what is true: a signal can be stale, can have been dropped, can arrive out of order, or can never have
come at all — so a crashed run and a quiet one look identical from outside, and the last event is a liar about
whether the work is done and how it ended.

### Why naive approaches fail

A live-only design silently drops the disconnected watcher and can never be trusted for completeness. A
record-only design cannot show live progress on a multi-hour run without re-reading everything, repeatedly, at
a cost that does not scale. Treating the last observed signal as the run's state turns every dropped message
and every crash into a confidently wrong answer about what happened. Merging the two channels naively produces
duplicates, gaps, or contradictions.

### What later layers must remember

Observation has to be designed as two channels made mergeable — a fast, lossy one and a complete,
authoritative one — with the durable record always the authoritative account, and with a run's true state
reconciled against that record rather than read off the last thing anyone saw. A watcher that missed a stretch
of a run must be able to recover exactly the stretch it missed.

### Not prescribed here

Delivery transport, the reconstruction surface, how a resumed watcher re-positions, how duplicates are
identified, completeness signals.

## What a run exposes ossifies under the systems that read it

### Reality

What a run leaves behind, and the shape in which it leaves it, is read by systems the authors do not control —
often built separately, on a different version, unable to load the producing application's definitions. The
moment any of that is parsed by outside code, its shape has become something other systems depend on, whether
or not anyone meant it as a promise. An internal an integrator reaches for because nothing else was offered
is, from then on, a coupling: when it changes, the integrator breaks, and it breaks silently, far away, with
no error at the point of change.

### Why naive approaches fail

Assuming the only reader of a run's record is the code that produced it is wrong the first time another system
integrates. Treating the shape a run is recorded or handed off in as private, because it was never documented,
does not make it private once external code reads it. "We can change it later" assumes a coordinated consumer
that, across independent systems on independent versions, does not exist. The result is either frozen
internals nobody can change, or silent breakage every time they do.

### What later layers must remember

Any surface that outside systems read — the record, whatever crosses a boundary between systems, the account
of what a run did — is a depended-on shape and must be treated as one: stated deliberately, readable without
the producer's code, and able to fail loudly rather than silently when a reader meets a shape it does not
understand. The alternative is not "no contract"; it is an undocumented contract that breaks without warning.

### Not prescribed here

Serialization formats, versioning mechanics, which surfaces are exposed, how incompatibility is signaled.

## Open-world evidence is unreliable by default

### Reality

In the evidence-bearing work this framework targets — not every application operates in the open world, though
the framework is shaped by those that do — inputs are not curated datasets. Discovery is not verification:
finding a claim does not establish that it is current, independent, or about the intended subject. Agreement
can be echo: many surfaces repeating one upstream filing look like confirmation. Retrieval silence is a scoped
observation, not absence: what was not found may simply not have been reachable from where the search looked.
Unresolved questions harden early: an identity hypothesis becomes a planning assumption, and every dependent
step inherits a commitment the evidence never earned. The asymmetry is sharp — a false merge entangles every
attribute of two entities and demands re-examination of everything downstream, while a false split is usually
repaired later at little cost. Bounded processing hides what was never examined: a result computed from the
first or highest-ranked portion of the material carries no mark of the contradiction sitting in the rest. And
upstream relationships do not all mean support — a note that caused a source to be fetched is not evidence for
the claim the source makes, and when relationship kinds flatten into one undifferentiated ancestry, review is
left guessing whether an earlier item supports a finding or merely caused the work that produced it.

### Why naive approaches fail

Treating search output as settled fact, counting agreeing sources without establishing independence, resolving
ambiguity because downstream code wants a single premise, and reading a clean retrieval as a clean record all
share one structure: they convert the absence of resolved evidence into the appearance of it. Each produces
outputs that sound appropriately confident and rest on nothing.

### What later layers must remember

In this environment a confident value is not better than an honest unresolved one. When uncertainty is erased
before evidence resolves it, dependent work inherits claims the evidence did not earn, and the final result is
wrong in a way no later step can detect from inside the chain.

### Not prescribed here

Verification methods, independence tests, identity-resolution policy, record shapes.

## Evidence found is not evidence used

### Reality

The most insidious failure in these systems is not that the right evidence was never found — it is that the
evidence was found, correctly assessed, recorded, and then absent from the working set of the later judgment
that needed it. Nothing fails loudly: the step proceeds, produces a conclusion that contradicts what the
system already knew, and the contradiction never surfaces. The pressure is two-sided. Too little reaches the
judgment, and it acts as if the evidence does not exist; everything reaches the judgment, and the relevant
item is buried under accumulated history that no stage was responsible for selecting. What is locally useful
and what is relevant downstream are different questions, and what each stage hands forward is itself a
judgment that can fail.

### Why naive approaches fail

Improvised context assembly — "the data is in the system somewhere" — guarantees the gap between availability
and presence. Forwarding everything to be safe recreates the same omission as an attention failure: the
needle is technically present and effectively lost. Both look fine in short runs, where there is little
history to lose things in.

### What later layers must remember

Availability somewhere in the system is not presence where the judgment runs. When what was gathered does not
demonstrably reach what depends on it, "no evidence was found" and "the evidence was never consulted" produce
identical outputs — and only one of them is honest.

### Not prescribed here

Handoff shapes, selection rules, context-assembly mechanics.

## Underspecified judgments fork into divergent honest answers

### Reality

The judgments these systems rely on are defined in words — relevant, risky, reputable, strong signal, good
enough — that sound concrete while permitting several valid readings. The author of the definition carries the
intended meaning privately; every reader completes it locally. Hand the same rule to two careful analysts in
separate rooms and they disagree; hand it to two model runs and they disagree the same way. Both outputs are
correct readings of the question as asked, and nothing in either output reveals that the question was the
defect.

### Why naive approaches fail

Lowering temperature, adding retries, switching to a stronger model, and rewriting the prompt all attack the
interpreter, and the interpreter is not the problem. An undefined decision rule cannot be made consistent by
tuning whoever applies it — at best the tuning hardens one accidental reading into apparent stability.
Agreement between runs proves convergence, not that the converged reading is the one the author intended.

### What later layers must remember

When a relied-on judgment is not operationalized — by rubric, threshold, example, or explicit boundary —
divergence is a defect in the question, not in the interpreter, and no downstream effort can repair it where
it surfaces.

### Not prescribed here

Rubric formats, threshold shapes, metadata fields, validation mechanics.

## Machine-speed authorship converges on whatever is visible

### Reality

These systems exceed hand-authoring capacity, so AI agents write the application code, at a rate no
review process fully inspects. Under ambiguity, capable agents produce locally coherent but mutually divergent
shapes. Whatever is visible in the codebase — including compromises and workarounds — reads as precedent: one
agent's shortcut becomes the next agent's template, until within a few generations the workaround is the
architecture. Where many shapes are syntactically valid, the locally simplest one wins, and the locally
simplest one is usually the most expensive to live with. And the authors do not persist: each session starts
cold, a lesson taught to one agent is already lost to the next, and the same class of mistake recurs across
agents and days until the structure they work in changes.

### Why naive approaches fail

Style guides and review vigilance are soft signals that lose to a visible concrete shape every time.
Instruction lists and per-author correction fail one step later: instructions decay as they accumulate and
vanish with the session, and there is no standing author to train. Offering flexibility plus guidance assumes
the author weighs the guidance; the actual authors copy the nearest working example. Planning to clean up
later misunderstands the speed: by later, the shape has been copied into more places than the cleanup can
reach.

### What later layers must remember

How authors must behave is the principles layer's question. What this frame fixes is the environmental fact:
at machine speed, whatever the visible surface shows is what the system becomes — the copying dynamic turns
today's shapes, good and bad alike, into tomorrow's architecture faster than any review can intervene.

### Not prescribed here

Surface design, enforcement mechanisms, review processes.

## Authors need a small surface; the guarantees need rich machinery

### Reality

The two audiences of the same system pull in opposite directions. Application authors — agents — need a
surface that is small, sufficient on its own, and learnable from what is visible. The guarantees this frame
demands — conclusions that survive challenge, state that survives failure, evidence that survives transit —
require machinery that is anything but small. The richness constantly wants to leak upward into the authoring
surface, and the simplicity constantly wants to amputate machinery the guarantees depend on. The pull is
sharpest around diagnosis: the author needs a minimal world in which the machinery is invisible, while the
operator diagnosing a failed run — an agent from the same population — needs the whole recorded truth. The
same system must serve both without letting either need destroy the other. And there is now more than one
outward surface, not just more than one audience: the surface an author builds against and the surface an
outside system drives and observes a run through are distinct contracts with distinct readers, and collapsing
them — making the author learn the operating surface, or making the integrator reach through the authoring
surface — bloats one and starves the other.

### Why naive approaches fail

Exposing the machinery lets authors entangle with internals, after which every internal improvement is a
coordinated migration across everything ever written against it. Hiding everything, including meaning, makes
authored work unreadable — an author who cannot tell what their code means cannot tell whether it is right.
Serving both audiences from one surface or one document bloats it for one reader and starves it for the other.

### What later layers must remember

The outward promise and the internal mechanism are different questions with different readers. Collapse them
and either users carry maintainer burden, or maintainers lose the room to change the machinery at all. And the
outward promise is no longer single: authoring a system and operating one from outside are separate contracts
for separate readers, and each must stay sufficient on its own without forcing its reader through the other. The
operating surface is also copied and learned the way the authoring surface is: at machine speed, whatever way of
running, observing, and re-running work is visible becomes the way the next agent reaches for, so a fragmented
operating surface fragments the operators' behavior exactly as a fragmented authoring surface fragments authored
code. And operating the work where it is developed and operating it in production are two modes of one surface,
not two products — a cheap development path that drops the recording and observation the production path has
cannot be inspected, re-measured, or studied the way production can, so the two must be the same loop differing
only in scale.

### Not prescribed here

What is public, file layout, API design.

## The forces reinforce each other

### Reality

One failure contains them all. A system gathers evidence correctly in one branch; a different branch reaches a
contradictory conclusion that is never flagged because the evidence did not reach it; compounding carries the
contradiction through consolidation and synthesis until it is well-supported in the final report; the origin
sits hundreds of judgments from the symptom; and the code that allowed it was machine-authored from a copied
shape no one chose. By the time anyone investigates, the run cannot be cheaply repeated and the substrate
that produced it has already shifted — and the live view that might have caught it dropped events midway,
while the crashed run it left behind has to be reconciled from the record before anyone can even say it
failed. And by the time anyone investigates, re-running to study it costs hours and real money, the loop they
reproduce it in is not the production loop it failed in, and the record they must measure against is only as
good as what each run thought to preserve. Each force amplifies the others: authorship speed compresses
inspection, lost evidence weakens the
checks meant to catch compounding, and unresolved ambiguity feeds both.

### Why naive approaches fail

Responses sized to one force pass review and fail jointly. A defect that any single force would have exposed
survives when several apply at once, because each response assumed the others' territory was clean.

### What later layers must remember

These forces are present from the first prototype, merely invisible at demo scale. Design against their joint
application; the default outcome of unstructured systems at production scale is all of them at once.

### Not prescribed here

Architecture, layering of the response, anything else.
