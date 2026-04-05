# Failure Modes, Incident Lessons, And Migration Playbooks

This document records the concrete failures, regressions, upgrade hazards, and recurring anti-patterns observed while building and refactoring applications on top of `ai-pipeline-core`, especially the deep-research systems used as reference applications. It is intentionally empirical: each item exists because it happened in practice, not because it is theoretically possible.

The goal is not to restate abstract principles. The goal is to show what failed, why it failed, how it was fixed, and what class of problem it represents so that future applications and framework work do not repeat it.

## 1. Prompt and Spec Contract Failures

### 1.1 Dynamic PromptSpec inheritance caused runtime failure
**Symptom:** AI-search provider task-generation crashed because dynamically created specs inherited from another PromptSpec instead of inheriting directly from `PromptSpec[...]`.

**Cause:** The framework enforces direct PromptSpec inheritance. A helper created subclasses from an intermediate spec class, which violated that rule.

**Fix:** Build dynamic classes that inherit directly from `PromptSpec[...]` and copy class attributes/field definitions explicitly.

**Prevention:** Never assume PromptSpec inheritance chains are acceptable. Dynamic prompt creation must preserve the framework’s exact class contract.

---

### 1.2 Roles were written as job titles instead of workflow context
**Symptom:** Provider-facing prompts were poor, vague, or full of internal project jargon. External search agents received briefs that assumed knowledge they did not have.

**Cause:** Roles were written as short labels like “provider-native request writer” instead of explaining what the model is producing, who consumes it, and what the consumer does not know.

**Fix:** Rewrite roles to explain the workflow boundary explicitly, especially when generating text for external systems.

**Prevention:** Review every role for “outside-in” framing. If the role reads like a résumé title, it is probably wrong.

---

### 1.3 Tasks described process instead of output
**Symptom:** Specs instructed the model to “analyze” or “review” without clearly constraining what concrete output should be produced.

**Cause:** Task text focused on internal workflow verbs rather than the exact artifact needed next.

**Fix:** Rewrite task text so it describes the deliverable, its scope, and its constraints. Keep process language out unless the process itself is the output.

**Prevention:** Prompt review should reject task descriptions that read like internal work orders rather than output specifications.

---

### 1.4 Warmup prompts produced real structured output and contaminated later reasoning
**Symptom:** Warmup calls generated full structured outputs, seeding later conversations with fabricated claims before real evidence arrived.

**Cause:** Warmups were implemented using the same structured output schemas as production calls.

**Fix:** Make warmups plain-text-only, brief, and non-authoritative. Their purpose is cache priming and orientation, not evidence production.

**Prevention:** Treat any warmup that can produce a first-class artifact as a correctness bug, not an optimization detail.

---

### 1.5 Missing output fields prevented the model from expressing required distinctions
**Symptom:** Important concepts such as suppressed alternatives, authority reduction rationale, re-extraction targets, or anchor updates had nowhere to go in structured outputs.

**Cause:** Output models were too narrow. The model had to omit information or hide it in prose.

**Fix:** Expand the structured models so the LLM can express the distinctions the downstream code actually needs.

**Prevention:** If reviewers repeatedly ask “where does the model put X?”, the schema is underspecified.

---

### 1.6 Decomposition fields were placed after decision fields
**Symptom:** Models made premature decisions and then rationalized them in later fields.

**Cause:** Structured output models put booleans or verdicts before the decomposition that should support them.

**Fix:** Put analysis/decomposition fields first, decisions last.

**Prevention:** Treat field order in structured models as part of prompt design, not a cosmetic choice.

---

### 1.7 Missing Rule subclasses caused silent prompt-contract drift
**Symptom:** Important checks existed only as prose in planning documents or comments, not as explicit Rule objects or explicit prompt constraints.

**Cause:** The prompt layer documented failure modes informally, while implementation assumed formal rule coverage.

**Fix:** Convert key failure modes into explicit rules or explicit prompt-contract sentences.

**Prevention:** If a behavior matters enough to audit, it needs a formal place in the prompt contract.

---

### 1.8 ProviderRequest prompts forced nested JSON instead of natural-language requests
**Symptom:** Query-generation specs produced provider API payloads embedded as JSON strings, leading to brittle, unnatural requests.

**Cause:** The LLM was asked to write transport format instead of the actual request content.

**Fix:** Have the LLM write the research brief or query only. Let code handle transport serialization.

**Prevention:** The model should never author protocol wrappers when the framework can do it deterministically.

---

### 1.9 Internal analytical language leaked into external provider briefs
**Symptom:** External providers received prompts referencing internal concepts like “Q1 gate”, “candidate comparison record”, “TG4”, or “identity resolution threshold”.

**Cause:** Internal task cards and provider-facing briefs were not separated cleanly. Downstream provider prompts echoed internal language instead of rewriting it for an external agent.

**Fix:** Introduce a two-tier output: one internal analytical brief and one external provider-safe brief.

**Prevention:** Any prompt that leaves the system boundary must be treated as a translation problem, not a copy-through problem.

---

### 1.10 Prompt-imposed query quotas and formatting requirements produced bad retrieval behavior
**Symptom:** Search providers were told to run exactly 5-10 or 30-50 queries, or to emit highly specific quote-heavy formats regardless of task complexity.

**Cause:** Prompt authors hardcoded numerical guidance and output mandates rather than letting the model scale effort to the problem.

**Fix:** Move from rigid quotas to strategy-aware, evidence-aware prompting. Use minimum expectations only when absolutely necessary.

**Prevention:** Hardcoded numeric prompt guidance should be treated as suspicious unless downstream code truly depends on it.

---

## 2. Evidence, Provenance, and Document-Graph Failures

### 2.1 Planning documents were treated as evidence
**Symptom:** Definitions, contracts, or planning summaries appeared in `supporting_dossier_refs`, `source_roots`, or equivalent provenance fields.

**Cause:** Prompt context and evidence context were not separated strongly enough, and code did not validate that provenance fields referenced only evidence documents.

**Fix:** Add a hard provenance boundary: non-evidence context must never appear in evidence provenance fields. Validate this in code.

**Prevention:** Any document that helps interpretation but is not evidence must be structurally excluded from evidence provenance.

---

### 2.2 Phantom provenance references pointed to nonexistent documents
**Symptom:** Documents listed `derived_from` references to temporary or synthetic artifacts that were never persisted.

**Cause:** “Safe” task cards or transformed documents were created for LLM context but then reused in provenance as if they were persistent artifacts.

**Fix:** Use original persisted documents in provenance. Keep transformed safe copies as transient context only.

**Prevention:** Provenance must only reference persisted documents that can be reloaded later.

---

### 2.3 `derived_from` and `triggered_by` were misused
**Symptom:** Documents that were causally triggered by earlier artifacts were recorded as if they were content-derived, or vice versa.

**Cause:** The semantic difference between “content source” and “control trigger” was blurred in implementation.

**Fix:** Reserve `derived_from` for substantive input artifacts and `triggered_by` for control-flow or decision-triggering artifacts.

**Prevention:** If removing a predecessor would not change document content, it probably belongs in `triggered_by`, not `derived_from`.

---

### 2.4 Assembled report provenance was severed
**Symptom:** The final assembled report existed as a plain Python string or transient concatenation, with no proper document provenance chain.

**Cause:** Final report assembly happened outside the document model.

**Fix:** Introduce an `AssembledReportDocument` and keep provenance through review, repair, and finalization.

**Prevention:** Final outputs must remain inside the document graph until the very end.

---

### 2.5 Synthetic pack IDs (`S1`, `S2`, …) collided and duplicated framework identity
**Symptom:** Pack IDs restarted, collided across rounds, and became a second parallel identity system next to the framework’s document IDs.

**Cause:** A custom source-label scheme was introduced instead of using stable document identity.

**Fix:** Remove synthetic pack IDs from routing and provenance. Use document identity as the canonical reference; only derive human-facing short labels where needed.

**Prevention:** Do not build a second identity system unless there is an unavoidable human-facing requirement.

---

### 2.6 Evidence content was duplicated in multiple document types
**Symptom:** The same markdown content lived in both metadata-heavy pack documents and separate evidence-content documents.

**Cause:** Metadata and content were not separated cleanly.

**Fix:** Keep metadata in one document type and the full text in one content-carrier document type.

**Prevention:** If two document classes carry the same large body text, the document model is probably wrong.

---

### 2.7 Report references and bibliography mapping were brittle
**Symptom:** Source citations failed to resolve, bibliography entries showed the wrong thing, or valid source references were marked unresolved.

**Cause:** Regex-based normalization and multiple alias systems were used to reconcile report citations to source artifacts.

**Fix:** Tighten the reference system, normalize exactly once, and preserve direct linkage from evidence artifacts to final report references.

**Prevention:** Any citation system that needs layered fuzzy matching is too fragile.

---

### 2.8 Citation URLs were silently dropped at the gateway boundary
**Symptom:** AI-search citations existed upstream but disappeared before they reached provenance pools and bibliography generation.

**Cause:** The consumer parsed the wrong fields from the fetch-service response, ignoring the attachment where citations were actually stored.

**Fix:** Read citations from the correct attachment and merge them with provider-reported metadata.

**Prevention:** Integration code must track the real wire format, not an assumed one.

---

### 2.9 Bibliography showed search queries instead of actual URLs
**Symptom:** AI-generated evidence cited actual websites, but the bibliography displayed the query text or synthetic identifiers instead of source URLs.

**Cause:** The bibliography builder used the request or pack origin field rather than extracted citation URLs.

**Fix:** Make bibliography construction prefer true cited URLs, with explicit fallback rules.

**Prevention:** Bibliography logic must be based on evidence provenance, not request metadata.

---

### 2.10 Group synthesis and section writing used the wrong evidence subsets
**Symptom:** Prior-round reports leaked into same-group synthesis, or section writers received irrelevant full-corpus evidence.

**Cause:** Evidence selection used weak filename conventions, substring matching, or broad tuples instead of explicit group/report assignments.

**Fix:** Use exact group assignments and explicit evidence lists.

**Prevention:** If evidence selection depends on string containment in filenames, it is a bug waiting to happen.

---

## 3. Provider and Gateway Integration Failures

### 3.1 The system started with zero providers configured
**Symptom:** A run spent money on planning and analysis even though no providers were available to gather evidence.

**Cause:** Startup validation did not enforce a minimum provider inventory.

**Fix:** Fail fast during initialization when autonomous mode has no usable providers.

**Prevention:** Execution prerequisites belong at startup, not after partial spend.

---

### 3.2 Planning validation stripped all acquisitions when provider inventory was empty
**Symptom:** Evidence acquisition plans lost all provider assignments, causing later steps to degrade or fabricate fallback behavior.

**Cause:** Planning validated provider types against configured instances rather than against the valid provider-type vocabulary.

**Fix:** Validate against the provider-type domain, not the runtime inventory, unless the business rule truly depends on current inventory.

**Prevention:** Never use a transient runtime set as the definition of a semantic type system.

---

### 3.3 One monolithic `ai_search` provider hid fundamentally different subproviders
**Symptom:** The system treated multiple AI search products as one generic provider, so prompt guides, routing, and request shaping never matched the actual execution engine.

**Cause:** Provider configuration collapsed `chatgpt`, `grok`, `gemini`, `perplexity`, `copilot`, and `google_ai_mode` into one logical bucket.

**Fix:** Model subproviders explicitly where behavior differs, or at least preserve subprovider identity through request generation.

**Prevention:** Distinct provider capabilities must not be erased at the interface boundary.

---

### 3.4 Wrong provider identifier sent to gateway
**Symptom:** AI search submissions failed with 422 because the downstream service expected provider names like `chatgpt` or `perplexity`, but the client sent internal IDs like `ai-search-default`.

**Cause:** Internal provider IDs were confused with upstream provider-family values.

**Fix:** Send the upstream’s expected provider names, not local aliases.

**Prevention:** Integration contracts should separate local identifiers from wire identifiers explicitly.

---

### 3.5 `additional_prompt` support existed upstream but was unreachable
**Symptom:** ChatGPT’s follow-up prompt capability was defined but never actually passed through the stack.

**Cause:** Models, request objects, and gateway payload generation omitted the field even though the downstream service supported it.

**Fix:** Thread the field from planning/specs through request generation into the gateway payload.

**Prevention:** Any upstream capability exposed by the provider integration should be tested end-to-end, not assumed by interface shape alone.

---

### 3.6 Web-fetch URLs were hallucinated by planning and request generation
**Symptom:** The system attempted to fetch invented URLs like `/terms`, `/about`, `/team`, or generic portals like SEC homepages and search tools.

**Cause:** Planning and task generation created fetch targets from language-model intuition rather than provenance-backed URLs discovered from evidence.

**Fix:** Enforce URL provenance. Web-fetch URLs must come from user input, discovered citations, extracted page links, or explicit verified seeds.

**Prevention:** URL generation must be treated as a provenance problem, not a prompt creativity problem.

---

### 3.7 `unknown` gateway statuses caused noisy failures and state loss
**Symptom:** Polling returned `unknown`, which the client treated as a mysterious warning or failure.

**Cause:** The upstream fetch service lost in-flight state because of short TTLs, process-local registries, or missing persistence for failure states.

**Fix:** Treat `unknown` as a real gateway state in the client and harden the upstream service so valid in-flight keys do not evaporate.

**Prevention:** Any provider gateway with async polling must persist state at least for the entire request lifetime plus terminal grace.

---

### 3.8 Fetch-service truncated or degraded content before analysis
**Symptom:** Page content ended mid-word, attachments were dropped, and downstream analysis saw incomplete evidence.

**Cause:** Upstream fetch-service character caps, downstream slicing, or attachment loss in integration code.

**Fix:** Remove all truncation in the evidence path and preserve attachments and citation metadata.

**Prevention:** Provider responses must be treated as evidence artifacts, not transport payloads to be shortened for convenience.

---

### 3.9 `FetchedPageDocument` / `FetchedWebsiteDocument` type mismatch
**Symptom:** Deserialization failed because a document returned by one service was loaded as a differently named type in another system.

**Cause:** Strict type checking in newer framework versions exposed a hidden document-name mismatch.

**Fix:** Align document class names or reconstruct documents without type-mismatched deserialization.

**Prevention:** Cross-service document interchange requires stable, shared document identity conventions.

---

## 4. Orchestration and Runtime Failures

### 4.1 Flow returned too many intermediate documents
**Symptom:** Flows returned all internal artifacts instead of only what downstream flows needed.

**Cause:** The flow was used as a dump of everything created rather than a handoff boundary.

**Fix:** Keep intermediate artifacts persisted by tasks, but return only downstream-required documents from the flow.

**Prevention:** Flow output is an interface, not a log.

---

### 4.2 Research round flow became a god-flow
**Symptom:** A single flow grew to ~880 lines with many helper methods, mutable shared state, duplicated pipelines, and in-flow document construction.

**Cause:** Phase logic, provider calls, matching logic, augmentation, synthesis, and round finalization were all absorbed into one orchestration unit.

**Fix:** Move work back into tasks and pure helpers; keep `run()` as a readable sequence of task calls.

**Prevention:** If a flow needs helper methods to remain readable, it likely owns too much work.

---

### 4.3 Direct provider API calls happened inside the flow
**Symptom:** Provider submission and polling occurred directly inside flow code, outside task boundaries.

**Cause:** Fire-and-forget provider logic was implemented as flow orchestration rather than as tracked tasks.

**Fix:** Wrap gateway submission and waiting in tasks so traces, retries, and metrics exist.

**Prevention:** Flows orchestrate tasks; they do not perform provider-side work directly.

---

### 4.4 `safe_gather` compaction corrupted pairings
**Symptom:** Results from failed fan-outs were zipped against original request lists, shifting reviews, evidence, or sections onto the wrong items.

**Cause:** Failure-compacting gather results were paired positionally with the original sequence.

**Fix:** Preserve index alignment or carry original item IDs through the gather result.

**Prevention:** Any fan-out/fan-in pattern must preserve origin identity even when some items fail.

---

### 4.5 Empty-evidence adjudication ran anyway
**Symptom:** Conflict/entity/verification tasks ran with no evidence resolved for the target issue, causing hallucinated resolutions.

**Cause:** No guard existed to stop adjudication when document routing failed.

**Fix:** Hard-stop those tasks when no evidence or supporting reports resolve.

**Prevention:** Any reasoning task that depends on evidence must fail closed when evidence is missing.

---

### 4.6 Section review and repair misaligned with section order
**Symptom:** Reviews were paired with the wrong sections or repairs were applied to the wrong drafts.

**Cause:** Section indices were taken from filtered sequences inconsistently.

**Fix:** Preserve explicit section identifiers and use them for all review/repair routing.

**Prevention:** Never rely on filtered list position when a stable section key exists.

---

### 4.7 Handoff documents broke because flow signatures were too narrow
**Symptom:** A handoff document named a prior artifact, but the next flow could not see it because runtime type filtering excluded it.

**Cause:** Flow `run()` signatures were narrowed to handoff types only, while name-based lookup expected the full accumulated document set.

**Fix:** Either preserve broader flow inputs or ensure handoff documents fully replace the need for named lookups.

**Prevention:** Narrowing signatures and introducing handoff documents must be designed together; doing only half causes runtime invisibility.

---

### 4.8 Structured documents used `.md` names and broke validation
**Symptom:** Structured content documents were named like markdown files, causing serialization/validation mismatches.

**Cause:** Document naming conventions were not aligned with content type.

**Fix:** Use `.json` naming for structured documents and `.md` only for markdown text documents.

**Prevention:** Document names are part of the contract; content type and filename must agree.

---

### 4.9 Final review crashed on unresolved source references
**Symptom:** Final report validation raised hard exceptions for unresolved references and killed the run.

**Cause:** Validation was implemented as crash-first rather than fail-soft diagnostics.

**Fix:** Log unresolved references and continue, unless the run explicitly requires fail-hard behavior.

**Prevention:** Review stages should detect and report defects before they become availability failures.

---

## 5. Concrete Research-Quality Failures From Real Runs

### 5.1 Wrong-entity merge: research.tech was merged with unrelated companies
**Symptom:** The system concluded the target was a Polish company led by the wrong people, merging multiple unrelated entities.

**Cause:** A hedged user hint (“may be X”) was converted into a settled planning definition, then propagated into task generation and evidence interpretation.

**Fix:** Treat identity hints as hypotheses until verified. Add wrong-entity correction paths and identity-first gates.

**Prevention:** Early identity assumptions are one of the highest-leverage failure sources in open-world research.

---

### 5.2 Strong evidence was found but never propagated
**Symptom:** The system discovered valuable sources (LinkedIn, registry entries, investor documents) but later summaries said “no evidence” or failed to use them.

**Cause:** Evidence packs were not correctly routed into findings analysis, or prompts failed to enumerate and bind evidence before synthesis.

**Fix:** Add evidence-pack-aware prompts, force pack enumeration before clustering/synthesis, and keep explicit source linkage through each stage.

**Prevention:** If useful evidence exists but later artifacts claim there is none, the problem is almost always context assembly, not retrieval.

---

### 5.3 Resquant run succeeded because fixes corrected evidence routing and citation handling
**Symptom:** After the routing and citation fixes, a resquant.com run produced a credible, citation-rich report with company identity, team, investors, and partnerships.

**Cause of previous failures:** Evidence existed but was flattened, citations were lost, and hallucinated names were not demoted cleanly.

**Fixes that mattered:** evidence-pack context routing, citation extraction, section repair, and better provider fan-out.

**Lesson:** Context correctness and provenance preservation mattered more than adding new reasoning steps.

---

### 5.4 research.tech run still underperformed despite more evidence
**Symptom:** Even after a much deeper run, the final report still missed or underused strong signals about the actual company and its people.

**Cause:** The system remained too conservative in evidence interpretation, prematurely stopped loops, and mixed confidence levels in group synthesis and report writing.

**Fix direction:** More explicit confidence separation, broader retrieval strategy generation, better contradiction handling, and richer direct-fetch exploitation.

**Lesson:** Once basic routing is fixed, the main quality bottleneck moves from data collection to interpretation policy.

---

### 5.5 Hallucinated personnel and fabricated funding claims were surfaced by weak source handling
**Symptom:** Fake names, fake rounds, and fabricated SEC-style identifiers appeared in intermediate evidence and sometimes made it into later summaries.

**Cause:** Search/deep research outputs were not sufficiently rewritten and de-biased before downstream use, and single-source claims were not isolated clearly enough.

**Fix:** Rewrite all non-direct provider results into canonical markdown evidence before clustering, and separate multi-source confirmed claims from tentative single-source claims.

**Prevention:** Raw AI-search output should never be treated as analysis-ready evidence.

---

## 6. Migration Hazards and Upgrade Playbooks

### 6.1 ai-pipeline-core 0.19 file-isolation rules broke monolithic modules
**Symptom:** Large files containing many flows, tasks, or specs failed import-time validation.

**Cause:** New framework rules enforced one `PipelineTask`/`PromptSpec`/`PipelineFlow` per file.

**Fix:** Split monolithic `flows.py`, `tasks.py`, and `specs.py` into packages with one primary class per file.

**Prevention:** Upgrade reviews must include framework validation changes, not just API surface changes.

---

### 6.2 Bare `Document` and convenience unions became invalid
**Symptom:** Task signatures using bare `Document` or broad convenience unions failed framework validation.

**Cause:** Newer framework versions tightened task input contracts.

**Fix:** Use precise document types or carefully justified unions that respect framework validation.

**Prevention:** Application code should avoid broad “accept anything” document signatures even when older versions tolerated them.

---

### 6.3 Post-class mutation of `input_document_types` became a framework smell
**Symptom:** Post-definition mutation was used as a workaround for runtime filtering and type overlap problems.

**Cause:** Flow and task contracts were not modeled cleanly, so runtime type routing was patched after class creation.

**Fix:** Introduce proper handoff documents or correct signatures rather than mutating class metadata after definition.

**Prevention:** If metadata must be monkey-patched after class definition, the contract is probably wrong.

---

### 6.4 Enum-to-string migration left stale `.value` calls
**Symptom:** After removing an enum abstraction, runtime code still called `.value` on strings.

**Cause:** Mechanical migration changed types but not all call sites, and checks were not run before shipping.

**Fix:** Remove stale enum assumptions and run type checks after each migration step.

**Prevention:** Type migrations should be followed immediately by static analysis and search for old access patterns.

---

### 6.5 Retry default changes caused silent behavior changes
**Symptom:** Framework defaults changed across versions, altering task/flow retry behavior without explicit application changes.

**Cause:** Application code relied on framework defaults instead of setting desired retry policy explicitly.

**Fix:** Pin retry settings in application settings, not in assumptions about framework defaults.

**Prevention:** Any framework upgrade should include a review of changed defaults, not just changed APIs.

---

### 6.6 Run ID limits surfaced only at runtime
**Symptom:** Long run IDs derived from user input failed validation only when the application ran.

**Cause:** Run IDs were assembled from unbounded query text.

**Fix:** Normalize and truncate run IDs before passing them to framework APIs.

**Prevention:** User-facing identifiers should be normalized at creation time, not validated after orchestration has started.

---

### 6.7 Pre-commit assumptions were wrong
**Symptom:** CI caught linting and formatting issues that local development missed, even though a `.pre-commit-config.yaml` existed.

**Cause:** The repository had pre-commit configuration files but hooks were not installed locally.

**Fix:** Install hooks explicitly or do not assume local hook coverage.

**Prevention:** “Configured” and “active” are different states for local tooling.

---

## 7. Empirical Anti-Patterns Worth Treating As Red Flags

These are recurring patterns that repeatedly produced bugs or quality regressions:

- Hard truncation of evidence context (`[:20]`, `[:5]`, `[:2000]`)
- Lexical heuristics used for semantic ranking (especially augmentation URL selection)
- Python-generated semantic status strings passed into LLM prompts
- Pairwise or section logic driven by list position rather than stable IDs
- Regex normalization used as the primary document reference mechanism
- Error/timeout payloads treated as evidence instead of as failure artifacts
- Synthetic identifiers layered on top of stable framework identity
- Final outputs assembled as strings outside the document graph
- Provider capabilities modeled too coarsely, losing real behavioral differences
- Prompt systems that assume external providers understand internal workflow context

---

## 8. What These Incidents Show

The strongest empirical lesson is that most catastrophic failures did not come from models being weak. They came from boundary mistakes:

- wrong things entering LLM context,
- correct evidence not reaching later stages,
- provenance being severed,
- provider integrations misreading wire formats,
- framework contracts being bypassed or patched around,
- and application code making semantic decisions that should have remained inside LLM reasoning.

The second lesson is that upgrading the framework exposes previously hidden design debt. Stricter validation did not create most bugs; it revealed them earlier.

The third lesson is that once evidence routing and citation handling are corrected, quality bottlenecks move upward: from retrieval plumbing to confidence management, entity disambiguation, and multi-round strategy design.