# Mission

## What we're building

ai-pipeline-core is a Python framework for building evidence-bearing analytical AI systems: applications that
execute long chains of model-mediated judgments over documents and open-world material, and deliver
conclusions that someone will challenge. The motivating examples are high-stakes analytical work — due
diligence on a company, risk assessment of a vendor, a source-backed research report, an audit — but the
frame is the class of systems, not any one domain. The pressures named in this layer recur across analytical
domains; the specifics of any single domain are not what this frame is about.

These systems do not just process steps. Most gather partial, contradictory, stale, and sometimes misleading
material from the open world; all of them judge what they have hundreds — and at production scale thousands —
of times in dependent sequence and present the result with the confidence of a finished document. Built
without a structural response to that reality, each application answers the same hard questions — what
counts as a durable result, what moves forward between stages, what happened when something went wrong —
locally and differently, and those incompatible local answers become the visible defaults the next author
copies. The framework exists so that one accepted set of answers carries across every application instead of
being re-invented, slightly differently and slightly wrong, in each one.

These systems are authored at machine speed, and once the application is written the binding cost is no longer
writing it but standing it up, running it, observing it, and improving it. The framework exists equally to keep
that path — from a declared application to a running, observed, and iteratively improved one — moving at the pace
the authoring does, so that bringing a system into service and correcting it never collapses back into
human-paced assembly between one change and the next.

## Primary users

AI coding agents author all of the application code, working under human direction. This is an
environmental fact, not an aspiration: at production scale these systems contain more coordinated steps and
dependency relationships than a human team can write, hold in working memory, or revise safely by hand. A
small team directs many agents across multiple applications; humans decide what the systems mean and carry
intent into the work through written specifications and review — never through code. The same agents also
operate what they build: the agent that later runs, verifies, diagnoses, corrects, re-executes, measures, and
ships a system is drawn from the same population as the one that authored it, and is a primary user of the
record a run leaves behind. Operating is not a slower, human-mediated phase that follows authoring; it is the
same loop, run end to end by agents at the same pace. A system its agents cannot launch, observe, correct, and
re-measure as fast as they can author it is one they cannot actually improve.

The framework therefore serves three consumers at once. Application authors and operators — agents — need a
surface they can use correctly without first reconstructing how the machinery underneath works. The people
accountable for the outputs need every delivered conclusion to remain explainable after the fact, by someone
who did not build the system. And the external systems that drive the work — together with the operators
behind them — need to launch a run, watch it while it runs, halt it when it must be stopped, recover it when
it is interrupted, and read its results and record back out from outside the process that executed it,
without having built the system or reaching into how it was built.

## Why this matters

A finished report from one of these systems is thorough, well-organized, and properly cited — and one sentence
in it can be confidently wrong because a judgment three hundred steps earlier misread a source, merged two
identities, or never received evidence the system had already found. At production scale the wrong answers
stop looking wrong: they are systematic, plausible, internally consistent, and supported by real citations.
Whoever stakes a decision on the output needs more than a polished document; they need the result to survive
challenge, and the people operating the system need a way to find where a challenged conclusion came from
without mounting a forensic project.

## Current failure

Without structural intervention, this class of system fails the same way every time. Demo-scale prototypes
look nearly flawless because their chains are short. Scaled to production, reliability collapses under
compounding error; evidence is found and then lost on the way to the judgment that needed it; agents authoring
at machine speed copy each other's workarounds until the workaround is the architecture; a finished or
in-flight run cannot be launched, watched, halted, recovered, or read from outside the process that ran it
without bespoke per-application wiring, so operating it means reaching into internals that were never meant
to be depended on; even when the analytical logic is sound, each application grows its own bespoke machinery to
stand itself up, launch, observe, and re-run its work, the loop it is developed on drifts from the loop it runs
in production, and the wait between changing something and observing the effect grows long enough that improving
the system stops being worth the cost; the evidence needed to understand a run that went wrong is scattered
across the separate parts that produced it — the machine, the executing machinery, the operating surface, the
application's own steps, and the services it called — each reached its own way, so diagnosis begins with manual
correlation before a cause can even be investigated; and when a stakeholder challenges one sentence, tracing it back through hundreds of
intermediate judgments takes hours per issue — when it is possible at all. These failures are named in
3-hard-parts.md;
together they are the default outcome of unstructured engineering at production scale, not the unlucky one.

## Scope

The framework's scope is what recurs generically across this class of systems: the structural questions named
above — what counts as a durable result, what moves forward between stages, what happened and why when
something goes wrong — and the realities of failure, recovery, and observing long-running work that every
application would otherwise re-solve for itself. The scope equally covers what the development loop
generically requires: these systems are continuously verified, diagnosed, corrected, re-executed, and
measured — by the same agents that author them — and a frame that covered only the running system would miss
half of what every application needs. That loop is in scope as a loop, not only as a set of capabilities:
keeping the path from a declared application to a running, observed result — and from a correction to a re-run —
fast relative to the work, uniform whether the work runs where it is developed or in production, and
re-enterable from a single part rather than from the start, is itself the framework's concern; so is preserving
enough of what each run did — its inputs, its results, and the account of how its cooperating parts behaved
while it ran — that past runs can be studied, diagnosed, and re-measured later, not only recovered. It covers, too, what every such system needs in order to be operated
from outside: launching a run, observing it while it executes, halting it when it must be stopped, recovering
it when execution or observation is interrupted, and reading its results and record back out — and standing
up the operational machinery that executes the work, so each application does not reassemble it. Applications
own domain meaning: what their artifacts represent, what their judgments ask, what counts as good evidence in
their domain, and what their consumers receive.

Out of scope for the framework itself: domain methodologies and scoring rules, the methodology and cross-run
systems built on top of it — those that accumulate, score, or reason over state across many runs — and any
promise about which conclusions are true. Operating and observing a single run, and the machinery that
executes it, are in scope; accumulating meaning across runs is not. Those out-of-scope concerns belong to the
applications and adjacent projects that consume the framework.

## Success boundary

Success means these systems remain buildable and accountable at production scale: an application with hundreds
to thousands of dependent judgments can be authored entirely by AI agents and reviewed by a small team; a
challenged conclusion can be traced to the evidence and judgments that produced it in minutes rather than
hours; an interrupted or partially failed run can continue without corrupting what was already done; a run
can be launched, observed, halted, recovered, and read back by an outside system through supported framework
surfaces, without bespoke per-application glue or coupling to internals; an application authored entirely by
agents reaches a running, iterating state in production within a single working session rather than over days of
assembly; operating it while it is developed is the same act, against the same record, as operating it in
production; studying or improving one part of a long, expensive run costs the work of that part rather than a
re-run of the whole; the accumulated record of past runs is a substrate an agent can re-measure to settle
empirically where a system can be made cheaper or better; and improving models make existing
applications better rather than exposing workarounds shaped around old weaknesses.

Failure means the forces win anyway: outputs that cannot be defended when challenged, applications too opaque
to modify safely, review reduced to sampling polished text, a framework so heavy that the systems it was
meant to enable are easier to build without it, the cost of standing a system up and launching it coming to
dominate authoring and analysis, the loop an application is developed on diverging from the one it runs in
production so that local success no longer predicts it, improvement that requires repeating a long run because
what it left behind is too thin to re-measure, or a runtime and observation surface so thin that every
consumer reconstructs it with bespoke glue or by reaching into internals.

## Fit boundary

The framework's strictness is justified by the forces in this frame, and only by them. Strong fit: long-chain,
document-heavy, evidence-sensitive work whose outputs face audit or challenge. Poor fit: conversational
assistants, single-call extraction or classification, latency-sensitive interactive products, and prototypes
that will never face production scale. Below the fit line the strictness costs more than it returns, and a
lighter tool is the correct choice.

## Drift signals

- A delivered conclusion is challenged and the team cannot say where it came from without reconstructing the
  run by hand.
- Reliability problems are answered by lowering temperature, adding retries, or re-running until outputs
  match, instead of finding the underspecified judgment upstream.
- Demo-scale success is offered as evidence of production readiness.
- Applications grow local copies of operational machinery — retry loops, persistence, routing — because the
  framework path was harder than rebuilding it.
- The framework accumulates per-application exceptions, switches, and parallel ways to express the same
  intent.
- Review of agent-authored application code degrades into sampling while defects keep surfacing downstream of
  where they were introduced.
- A human writes or edits application code to get a system working.
- A recurring agent mistake is answered with instructions or prompt changes instead of a change to the
  framework or its documentation.
- The standing rulebook agents are told to follow keeps growing; every durable rule in it is a correction
  that never landed in structure.
- Agents read framework internals, or improvise their own scripts to run, operate, test, diagnose, or measure,
  because the shipped surface was not sufficient on its own.
- A delivered run cannot be launched, watched, halted, recovered, or read back without per-application glue
  wrapped around the framework.
- A run that has gone wrong or is no longer needed cannot be stopped without killing the process or the
  machine that runs it.
- An external system reaches into framework internals to reconstruct or recover what a run did.
- A live view and a recorded view of the same run disagree, and there is no supported way to reconcile them.
- Each team that adopts the framework stands up and operates the execution machinery by hand.
- Operating a run where it is developed and operating it in production require different tools or mental models,
  so local success stops predicting production behavior.
- The wait between changing something and observing its effect grows long enough that agents stop iterating, or
  route around the framework to iterate faster.
- Improving the system means re-running work that already ran, or the record a past run left behind is too thin
  to re-measure it against a changed judgment, instruction, or model choice.
- Diagnosing a run means gathering and correlating separate partial accounts from the different parts that
  produced it before a cause can even be investigated.
- The framework grows a second interchangeable way to satisfy a concern it already supports one way, accepted as
  configurability rather than examined as divergence.
- Work on the framework can no longer name which force in this frame it serves.
