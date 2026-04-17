# Core Principles And Decision Rules

## Purpose

This document defines the normative principles that should guide every design and implementation decision in systems built on `ai-pipeline-core`. It is not an API reference and it is not a prompt-writing guide. It is the decision framework: what matters most, what tradeoffs are acceptable, what classes of mistakes must be prevented structurally, and how to divide responsibility between code and language models.

These principles exist to ensure that applications remain understandable, correct, debuggable, and resilient as both the framework and the models evolve.

---

## 1. The Framework Absorbs Complexity

### Principle
Application code should stay narrow and legible. The framework should absorb the hard parts: orchestration, retries, deployment transitions, tracing, persistence, validation, and common interaction patterns.

### Why
When every application re-implements the same control logic, every application accumulates the same bugs. Complexity spread across app code is not flexibility; it is duplicated liability. Centralizing reusable complexity is the only scalable way to get consistent correctness and to let application authors focus on domain logic.

### Decision rule
If the same class of logic appears in multiple applications, it belongs in the framework or in a reusable framework-adjacent layer, not in each app.

### Example
A developer should write “this task needs a critique pass” or “this document is a handoff artifact,” not custom orchestration code for retries, trace capture, or result persistence.

---

## 2. Applications Must Be Easy to Understand

### Principle
Application systems should be readable by an experienced engineer and by an AI coding agent without requiring archaeology. If the correct interpretation of the system depends on hidden conventions, brittle heuristics, or unstated context, the design is wrong.

### Why
The real cost of complexity is not just runtime behavior. It is the inability to reason about the system when it fails. Systems that cannot be understood quickly cannot be debugged safely.

### Decision rule
Prefer designs where a reader can answer:
- What is this component for?
- What input does it rely on?
- What output does it create?
- What decision is made here?
- What evidence justifies that decision?

If those answers are obscure, the structure is too complex or too implicit.

---

## 3. AI-Native Does Not Mean AI-Everywhere

### Principle
Use LLMs for open-world judgment, interpretation, synthesis, and ambiguity handling. Use code for exact structural checks, routing, persistence, invariants, and mechanical transformations.

### Why
LLMs are strong at making sense of messy reality. Code is strong at exactness. Systems fail when this boundary is inverted:
- Code tries to do semantic judgment with rigid heuristics
- LLMs are forced to simulate exact invariants or bookkeeping

### Decision rule
A decision belongs in code only when at least one of these is true:
1. It is an exact structural backstop.
2. It is a mechanical routing or persistence operation.
3. It enforces a true invariant that must hold regardless of model behavior.
4. It exists to preserve provenance, auditability, or data integrity.

Otherwise, the decision should remain LLM-first.

### Example
Choosing which cited URLs are worth direct fetch is usually an LLM judgment. Deduplicating identical URLs and normalizing them is code.

---

## 4. Open-World Semantics Belong to the Model

### Principle
Anything that requires interpreting the meaning of real-world content should be model-driven, not heuristic-driven.

### Why
The world is not closed, regular, or fully enumerable. Heuristics that appear to work on one domain silently fail on the next. Semantic shortcuts eventually become data corruption.

### Decision rule
Do not use code to decide:
- whether a page is relevant,
- whether a claim is truly contradictory,
- whether a URL is worth exploring,
- whether an entity match is meaningful,
- whether a result is “basically the same thing.”

Use the model for those judgments, then keep code responsible for exact consequences.

### Example
A token-overlap scorer for choosing augmentation URLs may look efficient, but it is the wrong abstraction. The model should rank relevance from full context; code should only deduplicate and dispatch.

---

## 5. Prompts Over Schema

### Principle
When a weakness can be solved by better prompts, better decomposition, or better conversation structure, that is preferable to expanding schema or hardening logic in code.

### Why
Many failures are prompt failures disguised as structural failures. Adding fields and code paths to compensate for underspecified prompts creates bloated architectures that are harder to maintain and still brittle.

### Decision rule
Before adding a new model field, validator, or fallback branch, ask:
- Can the model do this if asked more clearly?
- Can the task be decomposed into two smaller calls?
- Can the information be passed more explicitly in context?
- Can a follow-up conversation repair the missing part?

If yes, prefer prompt/conversation changes over schema growth.

### Example
A completion loop that asks only for missing outputs is often better than making the primary output schema larger and more fragile.

---

## 6. Structure Exists to Serve Code, Not to Satisfy Aesthetics

### Principle
Structured output is justified only when code must act on it deterministically.

### Why
Every structured field increases brittleness, validation cost, and migration burden. If code does not consume a field for routing, gating, persistence, or a strict invariant, that field should probably be prose.

### Decision rule
Add typed fields only when downstream code must:
- branch on them,
- validate them,
- preserve them as durable state,
- join on them,
- or enforce consequences from them.

Otherwise, prefer markdown or prose.

### Example
A typed `should_continue` field is justified because code branches on it. A typed “executive summary mood” field is not.

---

## 7. Typed Consequences Require Typed Outputs

### Principle
If the system is going to make deterministic downstream decisions based on an LLM judgment, that judgment must be surfaced in a typed field.

### Why
This is the only reliable way to let the model do semantic reasoning while still giving code a clean, auditable, enforceable interface.

### Decision rule
Whenever code needs to do something because of a model decision, the model must output:
- the decision in typed form,
- and, where appropriate, a typed justification or typed supporting structure.

### Example
If a review step can trigger repair, a boolean like `needs_repair` is appropriate. If provider use can be altered based on model judgment, the judgment must be explicit, not inferred from free text.

---

## 8. Typed Outputs Need Consistency Backstops

### Principle
Typed LLM outputs must be internally consistent. If a structured decision contradicts the model’s own explanation, the system should treat that as invalid and re-run or downgrade the result.

### Why
A model can easily emit `needs_repair=false` while still listing serious issues in prose. Without consistency checks, typed outputs become false certainty.

### Decision rule
For any typed control field, define what contradiction means and what the system must do when contradiction occurs.

### Example
If a structured review says “no repair needed” but lists substantial defects, the system should not trust the flag.

---

## 9. Search Outputs Are Observations, Not Facts

### Principle
Search-derived results are leads. They are not authoritative facts until corroborated or directly verified.

### Why
Search systems are noisy, biased, and often derivative. Treating search output as already settled collapses the distinction between discovery and verification.

### Decision rule
A search result may:
- suggest an entity,
- propose a relationship,
- reveal a source URL,
- or surface a claim.

It must not be treated as final confirmation unless a stronger source verifies it.

### Example
An AI search result naming a founder is a lead. A direct company page, registry filing, or primary source confirming that founder is the verification.

---

## 10. Consensus Routes; It Does Not Confirm

### Principle
Agreement across multiple secondary or AI-mediated sources is a routing signal, not proof.

### Why
Independent-looking outputs often share the same hidden upstream source. Counting them as separate confirmation creates false confidence.

### Decision rule
Use agreement to decide what should be checked next. Do not treat agreement itself as confirmation unless source independence is established.

### Example
Three AI providers repeating the same claim about a company founder may still trace back to one LinkedIn post. The right response is to fetch and verify, not to promote confidence automatically.

---

## 11. Never Merge Early

### Principle
When entity identity is ambiguous, preserve ambiguity. Do not collapse candidates into one entity until the evidence warrants it.

### Why
False merges are far more damaging than temporary duplication. Once two entities are merged, downstream conclusions are contaminated: team, investors, products, and risk claims all become unreliable.

### Decision rule
Treat user hypotheses, name similarity, and shared descriptors as provisional until hard identifiers or sufficiently strong evidence resolve the ambiguity.

### Example
If two similarly named companies appear in the same research space, the system should keep both live until identity is resolved rather than assuming one is the “real” target.

---

## 12. Identity Must Resolve Before Dependent Claims Harden

### Principle
Claims about roles, ownership, products, customers, or financials must not harden while the underlying entity identity is still unresolved.

### Why
Dependent facts become meaningless or misleading if attached to the wrong entity.

### Decision rule
When the subject identity is uncertain, downstream claims should remain explicitly conditional or blocked, not silently hardened.

### Example
A company’s customer list should not be treated as definitive if the pipeline is still unsure which legal entity or website actually corresponds to the target.

---

## 13. Direct Access Is Adjudication

### Principle
Direct URL fetching is not just another retrieval option. It is the adjudicative step that turns a discovered lead into primary evidence.

### Why
Search surfaces candidates. Direct fetch reads the actual source. Conflating the two causes pipelines to think they verified something when they only found where verification might happen.

### Decision rule
Direct-fetch targets must be provenance-backed. They should be derived from user input or from URLs discovered in evidence, not invented speculatively by code or by the model.

### Example
A homepage or registry search landing page is not the same as a verified record page. The system must distinguish discovery targets from adjudicative targets.

---

## 14. Search Silence Is Not Evidence of Absence

### Principle
Failure to find something through search is not proof that it does not exist.

### Why
Search visibility varies by language, indexing, geography, provider, and source quality. Treating silence as absence creates false negatives and blocks valid follow-up.

### Decision rule
Search silence can justify more targeted follow-up or explicit uncertainty. It cannot justify confident negative conclusions by itself.

---

## 15. Exclusion Requires Strong Justification

### Principle
Evidence should be included unless it is clearly non-evidentiary, clearly wrong, or structurally invalid.

### Why
Excluding something incorrectly permanently removes it from later reasoning. Including something noisy usually creates manageable downstream complexity; excluding something real can destroy the only trail to truth.

### Decision rule
Any exclusion rule must be defensible as one of:
- exact structural invalidity,
- obvious non-evidentiary content,
- or a reversible staging decision.

### Example
Timeout messages and tracking pixels can be excluded. Ambiguous pages with plausible relevance should usually remain in the evidence set and be judged by the model.

---

## 16. Error Chains Must Be Broken Early

### Principle
The system should detect and interrupt compounding mistakes before they become polished outputs.

### Why
Many failures are not single-point failures. They are chains: one bad assumption gets repeated, summarized, merged, and promoted until it looks authoritative.

### Decision rule
Add cheap interruption points where a bad interpretation could propagate widely:
- relevance gates,
- contradiction scans,
- targeted reviews,
- explicit uncertainty flags,
- or verification passes on high-risk claims.

### Example
A false person name repeated across several provider outputs should be challenged before it reaches the final report, not merely noted afterwards.

---

## 17. Section Filtering Is an Epistemic Guardrail

### Principle
Different output sections deserve different evidence standards. Not every claim belongs in the most prominent section of a report.

### Why
Reports can mislead even when the underlying evidence is present if certainty is not filtered by section.

### Decision rule
High-visibility sections should be more conservative. Low-confidence or unresolved material should appear only in sections designed to express uncertainty or open questions.

---

## 18. The Model Should See the Real Material, Not Python’s Summary of It

### Principle
When context size allows, the LLM should see the original material or the closest faithful representation of it. Python should not pre-chew meaning unless exact structural transformation is required.

### Why
Handwritten summaries, token heuristics, and partial context lead to information loss and bias. Modern large-context models are best used by feeding them more truth, not more code-generated interpretations.

### Decision rule
Avoid:
- trimming content for convenience,
- ranking by lexical overlap,
- summarizing before analysis,
- or dropping context because code thinks it is probably irrelevant.

If compression is needed, make the model do it explicitly.

---

## 19. No Truncation for Correctness-Critical Evidence

### Principle
Evidence content, provider outputs, and analytical inputs should not be arbitrarily truncated when correctness depends on them.

### Why
Hard caps trade away truth in ways the downstream model cannot detect. A dropped paragraph, citation, or footer may contain the key disambiguator.

### Decision rule
Never add truncation for convenience. If context reduction is necessary, use an explicit LLM transformation or a justified exact filter.

---

## 20. Derive, Don’t Trust

### Principle
Any field that can be computed exactly from typed upstream state should be derived by code rather than trusted from LLM output.

### Why
This prevents silent drift between a structured source of truth and a model-generated convenience field.

### Decision rule
If a value can be mechanically recomputed from canonical upstream data, code owns it.

### Example
Counts, normalized identifiers, exact URL sets, or source-reference lists should be derived, not model-authored.

---

## 21. Typed Derivations Must Be Traceable to Their Source

### Principle
If code derives a field or enforces an override, the reason must remain inspectable.

### Why
Silent derivation makes debugging impossible. The user may see a final value with no way to understand whether it came from evidence, code, or both.

### Decision rule
Derived or corrected fields should preserve enough context to explain:
- what changed,
- why it changed,
- and which upstream material justified the change.

---

## 22. Provenance Is a First-Class Asset

### Principle
Every meaningful output should preserve enough linkage to its antecedents that a later reader or model can inspect the evidence path.

### Why
Without provenance, reports become disconnected from the research process. That destroys trust, reusability, and debugging power.

### Decision rule
Do not break provenance chains with synthetic placeholders, hidden replacements, or untracked transformations. If a combined artifact is created, it should still point back to the artifacts it was built from.

---

## 23. Prefer Structural Backstops Over Heuristic Semantics

### Principle
When code must intervene, it should intervene with exact structural checks rather than approximate semantic guesses.

### Why
Structural checks are auditable and stable. Heuristic semantics are brittle and domain-specific.

### Decision rule
Good code-side checks:
- exact URL normalization,
- exact provenance membership,
- exact identifier matching,
- schema validation,
- contradiction checks on typed fields.

Bad code-side checks:
- lexical relevance scoring,
- guessed source importance,
- semantic value ranking,
- or open-world inference.

---

## 24. Monotonic Model Improvement Must Be Preserved

### Principle
Designs should get better as models improve. They should not lock in old limitations through rigid code assumptions.

### Why
A common failure mode is to encode today’s workaround as tomorrow’s permanent architecture. That caps future system quality.

### Decision rule
Do not hardcode ceilings around current model weaknesses if those ceilings suppress future capability. Use typed gates, prompts, and routing so better models can improve the system without major redesign.

---

## 25. Correctness Beats Cheapness

### Principle
Cost and latency matter, but they do not justify sacrificing evidence integrity, provenance, or truthfulness.

### Why
A cheap wrong answer is not a good trade. A system that hides or corrupts evidence to save tokens is optimizing the wrong thing.

### Decision rule
If a design choice reduces correctness in order to reduce cost, it is presumptively wrong. Cost-sensitive designs are acceptable only when they preserve the epistemic contract.

---

## 26. Simplicity Must Be Earned, Not Claimed

### Principle
Simplification is good only when it removes accidental complexity without deleting a real guardrail.

### Why
Many systems become “simpler” by removing the very mechanism that was preventing a known failure mode. That is false simplicity.

### Decision rule
Before removing a component, ask:
- What specific failure mode did it protect against?
- What now replaces that protection?
- Is the replacement equally strong, or are we silently accepting more risk?

---

## 27. The Framework’s Native Patterns Should Be Used Honestly

### Principle
If a problem can be solved within the framework’s intended patterns, prefer that over hacks, private hooks, or post-definition mutation.

### Why
Framework-integrity violations make upgrades painful and create fragile local conventions that others cannot reliably use.

### Decision rule
Prefer:
- explicit handoff documents,
- typed inputs/outputs,
- normal task invocations,
- native conversation continuation patterns,
- and documented extension points.

Avoid:
- private API dependencies,
- monkey-patching framework metadata after class definition,
- or hidden bypasses that only one application understands.

---

## 28. A Good Default Is: LLM Decides Meaning, Code Decides Consequences

This is the shortest operational summary of the whole philosophy.

- The **LLM** decides what a source means, which URLs matter, what is contradictory, what is unresolved, and how to synthesize findings.
- **Code** decides how documents are routed, how identifiers are assigned, how provenance is preserved, how exact invariants are enforced, and what downstream tasks should run because of typed model outputs.

Whenever a design feels ambiguous, start here. If code is trying to do meaning, it is probably wrong. If the LLM is trying to do exact bookkeeping, it is probably wrong. The architecture should preserve that boundary relentlessly.

---

## Final Decision Checklist

Before adding a rule, field, task, or fallback, ask:

1. Is this solving a real failure mode or just compensating for vague prompts?
2. Does this belong to the model’s semantic domain or code’s exact domain?
3. If code is involved, is it an exact backstop or a heuristic shortcut?
4. If an LLM judgment causes downstream behavior, is that judgment typed?
5. If a typed judgment can contradict its own explanation, is there a consistency check?
6. Does this preserve provenance and later re-inspection?
7. Does it remove or weaken an existing guardrail?
8. Does it permanently lose evidence when it could instead preserve it?
9. Does it remain valid as models improve?
10. Is it framework-native, or is it a workaround that will become future debt?

If a proposed change cannot survive this checklist, it does not belong in the system.
