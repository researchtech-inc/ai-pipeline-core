# Constraints

Constraints are boundaries, not preferences and not chosen mechanisms. Any acceptable solution must respect
every one of them; a solution that violates one is unacceptable regardless of how capable or elegant it is
otherwise. Each constraint states a pressure the environment imposes and leaves the response to later layers.

Behavioral rules for how humans and AI agents work on this project — authority, scope discipline, naming,
compatibility posture, coding baselines — live in the principles layer, not here. This file constrains
solutions; principles constrain authors. If the four problem files appear to conflict, dependent work stops
until the frame is repaired; later layers do not reconcile problem-frame conflicts locally.

## Delivered conclusions must survive challenge

The outputs of these systems are used for decisions someone is accountable for, and the people who rely on
them challenge specific sentences, not whole reports. Any acceptable solution must let a conclusion be traced
after the fact — by someone who did not build the system — to the evidence and judgments that produced it.

A solution in which the answer to "where did this number come from?" is rereading the code and guessing fails
this constraint, no matter how often its outputs happen to be right. How lineage is represented is not decided
here; that it must be recoverable is.

## Reliability must hold at production chain length

Evidence from short runs does not carry. A solution that looks dependable across ten dependent judgments can
still be unusable across five hundred, because the errors that matter at scale are systematic and plausible
rather than rare and obvious. Any acceptable solution must remain operable when dependent judgments number in
the hundreds and thousands.

Per-step quality alone cannot satisfy this constraint: remaining error multiplies through every dependent
step. A solution whose reliability story is "each step is quite good" has no reliability story at production
length.

## Correctness must survive machine-speed authorship

All application code in this class of system is written by AI agents, faster than human reviewers can
inspect it. Agents complete underspecified questions locally, copy whatever shape is visible, and diverge from
one another wherever more than one answer looks valid. They also retain nothing between sessions: a
correction delivered as an instruction is gone when the session ends, and the next author — statistically the
same author — makes the same mistake. These dynamics are environmental facts the solution must withstand, not
behaviors it may assume away.

An acceptable solution therefore cannot depend on every author exercising restraint or judgment correctly at
every site, nor on knowledge that is not visible where its authors work — whatever the solution needs its
authors to know must be carried by the written surface they work against, because no other channel reaches
them. Instructions, vigilance, and author memory are not enforcement mechanisms: an acceptable solution makes
an incorrect shape inexpressible or rejects it before it runs, and treats the same misuse recurring across
authors as evidence of a defect in the surface, not in the authors. A solution that is correct only in the
hands of a careful, fully informed author fails under its actual authors.

## Evidence integrity is not negotiable in transit

In the evidence-bearing work this framework targets — not every application operates in the open world, but
the framework is shaped by those that do — material arrives partial, contradictory, duplicated, and sometimes
misleading, and its meaning depends on source, scope, and lineage. Whatever else a solution does, evidence
must not be silently dropped, truncated, flattened, merged, or resolved ahead of what it supports on its way
from source to conclusion.

An acceptable solution does not trade that relationship away for cost, speed, or local simplicity. A cheaper
run that quietly weakened the link between a conclusion and its evidence did not get cheaper; it moved the
cost into the conclusion.

## Analytical state must stay trustworthy through failure and recovery

Runs are long, expensive, and partially fail as a matter of course: a provider stalls, a process dies, a step
produces garbage on the third hour of a four-hour run. The substrate itself is volatile — models differ in
capability, providers change behavior beneath running systems, and a dependency that worked at hour one can
degrade by hour three. Any acceptable solution must let work be retried,
resumed, and inspected later without prior results changing meaning, being double-counted, or quietly
disappearing. And because a crashed or interrupted run cannot be trusted to announce how it ended — the last
signal anyone saw may be stale, may have been dropped, or may never have come — recovering such a run means
reconciling it against what was actually recorded, not inferring its fate from elapsed time or the last thing
observed.

A solution in which recovery means starting over, in which a retry can observe state half-modified by a
failed sibling, or in which a run's outcome is guessed from the last event instead of reconciled against the
record, fails this constraint before its first production incident.

## The record is the sole source of truth, and recording must keep pace

What a run did is known to every later reader only through what was recorded; work that happened but was not
recorded did not happen as far as anything downstream can tell. The record therefore cannot be a best-effort side
effect of the work — it is the work's only evidence of itself. It must hold every operation a run performs,
preserved additively rather than overwritten, so that what was true earlier stays recoverable after later work
changes the picture. And a run that cannot record what it is doing cannot be trusted to continue: advancing on
state held only in a process's memory manufactures exactly the silent gap — a conclusion with no recorded basis —
that the rest of this frame exists to prevent.

The substrate that holds the record must also keep pace with the work. Production runs issue work at machine speed,
many thousands of steps per minute, and every one of them produces evidence that has to be recorded as it happens.
A substrate that cannot absorb that rate becomes the bottleneck, and the pressure to keep the run moving becomes
pressure to drop, defer, or skip recording — which is the unsaved-work failure above, arriving through the back
door.

A solution in which recording is lossy under load, in which a recording failure is swallowed so the run can limp
forward on unsaved state, or in which earlier recorded facts are replaced rather than preserved fails this
constraint. How the record is stored, how durability and throughput are achieved, and what halts a run whose
recording fails are not decided here; that the record is authoritative and additive, that it keeps pace, and that
a run cannot continue past a recording failure, is.

## Correctness must not depend on where work runs

Production work spreads across processes and machines, and inspection happens elsewhere and later than
execution. A solution whose correctness depends on which process held which local memory fails three ways at
once: in distribution, in recovery after a crash, and under later inspection of what happened.

This constraint does not choose an execution model. It states that hidden process-local dependencies are
unacceptable in systems that must be distributed, recovered, and audited.

## Operating a run must not depend on where it runs

Production work spreads across many machines, but the same work is developed, run, and studied on one, and the
two must be one system to operate, not two. Any acceptable solution must let the same actions — launch, observe,
halt, recover, and read a run — work the same way whether the run executes in one place for development or across
many for production, and must not require production-scale machinery to operate the work where it is developed.

This is the operator-facing twin of the constraint above: that one promises the same *answer* regardless of where
work runs; this one promises the same *operation*. A solution with one way to operate a run during development
and a different way to operate it in production makes the developed system and the running system two different
systems, so what is verified during development is not what ships, and the divergence surfaces at the most
expensive moment to discover it. How operation is kept uniform across placements is not decided here; that it
must be is.

## Observation must survive disconnection and missed delivery

These runs are long and expensive, and whoever watches one connects late, drops, and reconnects. Any
acceptable solution must let work be observed while it is still running — not only after it ends — and must
let a watcher that missed part of a run recover the part it missed. A dropped or delayed signal is the normal
case, not the exception, so observability cannot rest on every watcher seeing every moment as it passes.

A solution in which a watcher that disconnects loses the run, or in which progress is invisible until the run
completes hours later, fails this constraint. How a live signal and a recoverable history are reconciled is
not decided here; that both must exist, and that the gap between them must be closeable, is.

## Work launched from outside must stay governable from outside

A run is launched in one place and may need to be stopped from another: its inputs turn out wrong, it is no
longer needed, or it is spending without bound. Any acceptable solution must let a run launched from outside
be halted from outside before it completes, without a human reaching into the machine that runs it and
without corrupting what was already recorded. Because the substrate is metered, a run that cannot be stopped
from outside turns every wrong launch into a standing cost the operator cannot recall.

A solution in which a wrong or unwanted run can be ended only by killing the process or the machine that holds
it, or in which stopping a run corrupts or loses what it had already recorded, fails this constraint. How
control is exposed is not decided here; that an in-flight run stays governable from outside is.

## The record must be readable without the code that produced it

What a run leaves behind is read by systems other than the one that produced it — often by a system built
separately, or against a different version, that cannot load the producing application's definitions. Any
acceptable solution must let an independent reader understand a run's results and record without importing
the code that wrote them and without depending on internal shapes that were never meant to be relied on. An
independent reader does not only inspect one run; it compares many — past runs against each other and against
later ones — to settle questions the producing application cannot answer alone, so the record must preserve
enough of what each run did, its inputs and its results, to be re-measured and compared across runs, not only
read one at a time.

A record that can be understood only by re-running or re-importing the producing application is not externally
readable, and any internal an outside reader is forced to reach for becomes a coupling that breaks silently
when it changes. What the externally readable surface looks like is not decided here; that one must exist, and
that it must stand on its own, is.

## A run must be understandable from one diagnostic account

The work of a single run is carried out across many cooperating parts: the machine it runs on, the machinery
that schedules and executes it, the surface through which it is operated and observed, the application's own
steps, and the external services it calls. Each part emits its own account of what it did. Any acceptable
solution must make those accounts reachable together, correlated to the run, as one coherent account, so that an
agent diagnosing a run does not have to find, reach, and reconcile a separate, differently-shaped record of
activity for each part.

This is distinct from the constraint above: that one is about an outside reader understanding a run's results
and record; this one is about the operational account of how the run behaved not fragmenting by part. A solution
in which the application's account lives in one place, the executing machinery's in another, the operating
surface's in a third, and an external service's in a fourth — each reached its own way — forces the diagnosing
agent into exactly the cross-part forensic reconstruction this frame exists to eliminate, and an agent with
bounded working context cannot assemble such a scattered account at all. How those accounts are unified and made
reachable is not decided here; that all of it is reachable together, as one coherent account tied to the run, is.

## One identity must address a run across every surface and over time

A run is launched in one place, observed in another, recovered in a third, and read back in a fourth, and the
same run must be the same thing through all of them. Any acceptable solution must give a run one stable
identity that addresses it coherently wherever it is launched, observed, recovered, or read — and that stays
disambiguable when the same identity is reused across attempts over time.

A solution in which each surface invents its own way to name a run, or in which a reused identity cannot be
told apart from the run it reused, leaves every integrator to build its own correlation scheme and every
recovery to guess which attempt it is recovering. How identity is represented is not decided here; that one
identity must carry across surfaces and stay disambiguable over time is.

## Generic operational burden cannot land on application authors

Every application in this class needs the same operational behaviors: failure handling, persistence, recovery,
pacing against external limits, observation of long-running work — and, just as generically, standing the system
up to run, launching and re-launching it, studying and re-measuring what past runs did, and reconciling a
crashed one. These are real, recurring, and expensive to
get right. They include the money itself: this work spends at machine speed, and any acceptable solution
keeps that spending bounded and attributable. They include, too, the operational machinery these systems run
on: the work executes on a stack of cooperating services that is itself failure-prone to assemble and operate,
and standing it up correctly is the same generic problem for every application. When each application carries
these concerns — or each adopter reassembles that stack by hand — a small team multiplied across many
applications re-solves them inconsistently, and application code stops being about its domain.

Acceptable solutions must not require application authors to manage these concerns in order to get correct
behavior. Where the burden lands instead is a later-layer decision; that it cannot land on every application
author is decided here.

## The change-to-result loop must stay short

When application code is authored and corrected at machine speed, across many applications at once, the binding
limit on throughput stops being the time to write the work and becomes the time to launch it, observe its
result, and re-run it after a change. Any acceptable solution must keep the path from launch to observable
result, and from a correction to a re-run, short relative to the work itself, with no per-application assembly of
operating machinery standing between a change and its observed effect.

Because a production run takes hours and spends real money, this also forbids paying for the whole run to study
one part of it: the unchanged majority of a long run must be reusable, so that correcting or re-measuring one
part costs the work of that part, not a re-execution of everything around it. This is distinct from *Analytical
state must stay trustworthy through failure and recovery* above, which protects the meaning of prior results
across a resume; this constraint protects the economics of iteration. A solution whose ordinary loop spends more
on standing up, launching, and waiting than on the work — or in which studying one step of a long run means
paying for the whole — has moved the bottleneck rather than removed it.

## The development loop must be operable by agents

The systems in scope are not only authored by AI agents; they are verified, diagnosed, corrected,
re-executed, and measured by them. Any acceptable solution lets its authors reproduce, inspect, and act on
past work without a human mediating, and a failure signal must carry enough for an agent to correct course
from the signal alone. Routine verification and diagnosis cannot require a person at a keyboard or a manual
reconstruction across hundreds of interactions. Because these runs are long and expensive, an agent operating
that loop must also be able to re-enter the work from a single step rather than from the start, exercise one unit
against its recorded inputs, and measure a change against what a past run actually did — across the many runs
already accumulated, not only the one in front of it — so that settling empirically where a system can be made
cheaper or better never requires re-running work that already ran. And because diagnosis is part of that loop, an
agent must be able to work from the run's diagnostic account as a whole rather than first gathering and
reconciling fragmented evidence from each part by hand — routine diagnosis that depends on a human assembling
that account defeats the loop before the agent reaches it.

A solution whose standard loop works only when a human reads, interprets, and relays what happened has
reintroduced the bottleneck this class of system exists to pass. What equips the loop is a later-layer
decision; that an agent can operate it end to end is decided here.

## Structural soundness must not impersonate analytical correctness

Structure can make work traceable, typed, validated, and reproducible. It cannot make conclusions true. A
system can be impeccably recorded and systematically mistaken, and these systems fail most expensively when
mechanical integrity is mistaken for analytical quality — when a result is trusted because everything around
it checks out.

Any acceptable solution must keep that boundary visible: nothing in it may present structural soundness as
evidence that the analysis is right. What structure buys is named honestly — diagnosis, repair, audit — and
nothing more.

## Strictness must stay justified by the forces

The constraints above warrant real strictness for the class of systems in scope. Outside that class, the same
strictness is dead weight. An acceptable solution stays aimed at the mission's fit boundary, and any strictness
it imposes must be able to name the force it answers. Strictness that cannot is cost wearing a safety costume,
and it accumulates until the framework defeats its own purpose.

## What this layer does not decide

These constraints do not choose the public surface, vocabulary, storage, execution model, validation
mechanics, the shape of any verification, diagnosis, or re-execution tooling, how lineage, state, and
recovery are represented, how the loop is made fast or how already-done work is reused, how past runs are
re-measured or compared, how the diagnostic account of the cooperating parts is unified, stored, or reached, or
how operation is kept uniform wherever the work runs. Those answers belong to the
contract and design layers, judged against this file.
Rules for author behavior belong to the principles layer.
