# Meaning and State

## Models judge meaning; code enforces mechanics

Use models for semantic work: interpreting evidence, comparing explanations, assessing support, synthesizing
meaning, classifying open-world material, and drafting reasoning-bearing prose. Use code for mechanics: counting,
sorting, routing on closed values, validating shape, enforcing bounds, persisting, retrying, pacing, replaying, and
reading the record.

Do not ask code to infer domain meaning from strings, rendered prompt context, filenames, identifiers, or incidental
field names. Do not ask models to carry mechanical obligations the framework can type, bound, validate, or record.
When a value controls routing or delivery, give it a typed field. When reasoning matters, keep the explanation
visible and evidence-backed.

## Operationalize the judgment; fix the question, not the interpreter

Underspecified judgments fork into multiple honest answers. Terms such as relevant, risky, reputable, sufficient,
strong, current, independent, and good enough need rubrics, thresholds, boundary cases, examples, or explicit
failure behavior before a model-mediated judgment depends on them.

When model outputs diverge because the question permits several readings, repair the prompt contract, methodology,
body file, output model, or application specification. Do not treat lower temperature, retries, stronger models, or
validation loops as the durable fix for an undefined question. Tuning can stabilize an accidental reading; it cannot
make the question correct.

## Reasoning precedes structured consequence

When a model-authored response contains both explanation and a routed consequence, the model should reason from
evidence before committing to the value code will route on, and a later reader should see why the route was taken.

A bare flag, score, or closed status with no visible rationale is not enough for analytical work. If the reasoning
does not matter, the value may be mechanical and should be code-owned. If the reasoning matters, preserve it as
model-authored content, usually with document-backed citations when it makes evidence claims.

## Preserve ambiguity as typed state; abstention is a valid result

In open-world evidence work, an unresolved state is often the honest answer. Insufficient evidence, contradiction,
blocked retrieval, pending external work, degraded item failure, and out-of-scope material must be represented as
typed state when downstream work depends on them.

Do not collapse unresolved states into defaults, guesses, empty strings, low scores, dropped items, retried
abstentions, or fabricated conclusions. A well-formed abstention is success when the judgment has no supportable
answer. A degraded item is recorded as a typed failure document when the fan-out is designed to survive it. Expected
business failures are data; infrastructure failures use the typed exception channel.

## Evidence is never silently lost

Evidence available somewhere in the system is not evidence present where a judgment runs. Each prompt contract,
task, phase handoff, document bundle, citation, and output boundary must deliberately carry the evidence or state
its consumer needs.

Do not rely on broad active state, durable-store browsing, rendered prompt text, logs, nearby documents, copied
ancestry, or "the data is in the system somewhere" as a handoff. Scope evidence deliberately; pass it as document
inputs; record immediate provenance with the correct factory; keep cited prose tied to document-backed citations;
and name phase outputs that later phases actually consume. Integrity is not traded for cost, speed, or local
simplicity.

## Advance durable state by addition; provenance and identity stay framework-owned

Framework and application code advance durable state by creating new recorded artifacts through supported surfaces,
not by mutating prior state in place. A revision, summary, correction, or replacement is authored as new recorded
state linked to the prior artifact, not as a patch that rewrites what earlier work left behind.

Application code does not parse document or run identities, construct them from business values, copy content to
import a document, mint provenance by hand, or flatten ancestry into a new document. It records immediate
relationships through the supported factories and relies on the framework-owned record, identity, and provenance
machinery to recover the full graph.

## Do not encode current model limits as architecture

Models improve. Framework architecture should not fossilize workarounds for today's model weakness when the
framework can instead express the real boundary: typed inputs, citations, validation, bounded continuation,
abstention, replay, and measurement against the record.

Do not simulate native framework capabilities in prompt text or local code. Do not ask prompts to manage retry,
cache, provenance, routing, identity, store lookup, run control, or execution state. If the framework owns the
capability, use the framework surface; if the surface is missing, treat it as a framework change rather than a
prompt workaround.
