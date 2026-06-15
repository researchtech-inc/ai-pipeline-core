# Foundations

The authoring and structure of this `docs/` corpus are governed by the Documentation Orchestration System. This
layer does not restate how to write doctrine, and corpus-authoring defects are repaired under that system. This
layer governs how ai-pipeline-core is engineered, reviewed, operated, and evolved, including how agents consume
accepted doctrine while doing that work.

## Use the 2026 agent baseline

ai-pipeline-core is built for capable 2026 agents: large-context, cheap enough to use routinely, able to reason over
prose and code, and responsible for both authoring and operating applications. Work on the framework assumes that
baseline unless an accepted obligation requires otherwise.

Older mitigations are rejected by default: RAG over a directly loadable doctrine corpus, chunked governing context,
long human-paced migrations, compatibility scaffolding for project-owned surfaces, and broad configuration menus that
exist mainly because weaker agents would have needed them. When the framework owns a surface and no external promise
requires the old shape, rewrite or regenerate it to the accepted shape within recorded scope.

This principle does not license breaking accepted contracts, stored data, public schemas, audit records, delivered
artifacts, integrations, or explicit maintainer requirements. Those are obligations, not stale mitigations.

## Doctrine is working context, loaded whole

Accepted doctrine is operating context. Agents do not treat it as passive reference material, and they do not let
task history, current code, generated summaries, or training priors substitute for it.

When doctrine governs a task, selected files load whole. Search hits, excerpts, summaries, and isolated headings may
help locate the right file, but they are not the governing context. The scope, boundary, examples, non-promises, and
local consequences that make a rule safe to apply live in the full file. Selectivity happens between files and
surfaces, not within the file once selected.

If a task cannot be done from directly loaded governing files at this corpus scale, that is a doctrine or workflow
defect to repair through the doctrine-authoring process. It is not permission for framework work to introduce a
hidden retrieval path for authority.

## Architectures must bound error propagation

Long analytical systems fail multiplicatively. A small upstream judgment defect, omitted source, underspecified
rubric, or wrong handoff can propagate through hundreds of dependent steps and surface only as a plausible final
answer. Stronger models and retries reduce local error; they do not remove the structural compounding.

Framework architecture must therefore bound propagation paths. Declare run shape before execution. Validate before
work starts. Bound fan-out and loops. Make handoffs explicit. Put verification, replay, and inspection at the
surfaces where downstream work depends on earlier judgments. Prefer loud refusal over silent degradation when a
shape cannot preserve the record, provenance, bounds, or evidence needed downstream.

An architecture whose correctness story is "each step is good enough" is not acceptable for production chain
length. It must also explain how defects are contained, found, repaired, and re-measured without re-running the whole
system.

## Structural soundness is not analytical correctness

The framework can make a run typed, validated, durable, bounded, traceable, replayable, and externally readable. It
cannot make the analysis true. A clean record can preserve a wrong conclusion perfectly.

Never present passing validation, complete provenance, successful replay, a coherent trace bundle, or agreement among
framework checks as evidence that a conclusion is analytically correct. Those properties buy diagnosis, repair,
audit, and recovery. Domain truth remains the application's responsibility and the reviewer's judgment.

Do not use green tests or passing regression checks to dismiss analytical drift. A passing test proves only the
behavior the test asserted against the recorded substrate; it does not prove the analysis remains accurate for a new
domain, source environment, methodology, model, or consumer need.
