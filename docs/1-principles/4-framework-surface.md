# Framework Surface

## Design authoring and operating surfaces for AI agents

The primary authors and operators are AI agents. The authoring surface, operator CLI, advanced programmatic surface,
runtime API, tests, and diagnostics must be usable by agents without reconstructing framework internals.

This means small, explicit, stable surfaces; fixed homes for roles; structured machine-readable outputs; diagnostics
that name the concrete fix; and supported paths for launch, observe, halt, recover, inspect, replay, test, sync, and
deploy. A surface that requires ordinary authors or operators to inspect internals, wrap the framework in local
scripts, or infer state from logs has failed its reader.

## One correct shape per intent; one supported way per concern

For ordinary framework concerns, there is one supported shape. The current accepted contract gives concrete
instances: durable state is authored through documents, model-mediated judgment through prompt contracts, execution
boundaries through tasks, business stages through phases, complete run shape through pipelines, operation through one
`ai-pipeline` command, and child pipelines through one authored boundary. Those examples are contract-owned; the
principle is that the framework does not offer parallel shapes for the same ordinary intent.

Offering interchangeable ways to satisfy the same generic concern creates divergence that agents copy. Optionality
earns its cost only when it occupies one already-stable role without adding a new author or operator shape, as model
provider choice does behind `AIModelRef`. Otherwise, choose one supported way and make the wrong alternatives fail.

## Visible defaults are copied

Agents copy the shapes they see in `ai-pipeline init` scaffolds, examples, fixtures, tests, prompts, generated
packages, CLI output, runtime examples, and current code. A wrong shape shown as a neutral example becomes a future
default faster than warnings can contain it.

Make the accepted current shape the visible default everywhere the framework teaches by example. Obsolete,
transitional, workaround, legacy, or invalid shapes may appear only as explicit anti-patterns paired immediately
with the correction. When the accepted shape changes, update or remove the visible old shape rather than preserving
parallel examples.

## Established meanings guide framework names; interfaces tell the truth

Names teach. Use established meanings for established concepts, and use project-specific names only when the concept
is genuinely specific and the boundary is visible where the name appears.

Framework interfaces must tell the truth about what they carry. For example, current accepted surfaces distinguish
durable state from convenience wrappers, run-varying configuration from environment and document state,
document-backed support from citation strings, prompt results from recorded cost and transport metadata, and run
state from unit status. Misleading names and bag-shaped payloads invite agents to smuggle old concepts into new
work.

## Correct the framework, not the agent

Recurring misuse by agents is evidence about the framework surface. The durable correction is not a longer
instruction to the next agent; it is a change to the contract, API, scaffold, diagnostic, validator, example, test,
tool output, or implementation procedure that made the misuse possible or attractive.

Instructions can steer one task. They do not persist across sessions. If the same error recurs, repair the visible
surface or the rejection point so the next author sees the right shape or is stopped before the wrong one runs.

Land the correction in the framework, in the most preventive form available.

## Make wrong shapes inexpressible; automate correctness over vigilance

Prefer structures that prevent invalid work from being expressed, and checks that reject invalid declarations before
they run. Import-time validation, plan-compile validation, typed boundaries, closed states, frozen boundary models,
bounded fan-out, explicit provenance factories, and structured CLI diagnostics are framework tools for making the
correct path easier than the workaround.

Do not rely on review vigilance, prompt warnings, naming conventions alone, or human memory for generic correctness
properties the framework can enforce. When an invalid shape reaches execution or a reviewer must detect the same
mistake repeatedly by inspection, move the check earlier or make the shape unavailable.

Prefer corrections in this order: make the wrong shape inexpressible; reject it early through validation; catch it
through static analysis; fail it at runtime with a concrete fix; only then rely on review instruction. The later the
correction acts, the more agent work and review attention it wastes.

Every author-facing failure should name the fix. A diagnostic that tells an agent only that something is wrong but
not how to repair it slows the development loop and pushes agents toward internals.
