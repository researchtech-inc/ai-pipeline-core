# LLM Interaction And Prompt Design

This document defines how applications built on `ai-pipeline-core` should interact with models. It covers PromptSpec design, role and rule construction, conversation patterns, provider-specific prompting, structured-vs-markdown output choices, and context composition. The goal is to make model behavior predictable, high-quality, and debuggable without shifting semantic judgment into Python.

---

## 1. The Purpose Of The Prompt Layer

The prompt layer is where semantic work happens. The model should decide meaning, interpret evidence, detect conflicts, rewrite noisy inputs, and produce usable analytical outputs. Code should only enforce structural invariants, route documents, preserve provenance, and make exact, deterministic checks.

A good prompt layer does three things well:

1. It gives the model the right context, in the right shape.
2. It asks for the right deliverable, with the right level of structure.
3. It constrains failure modes explicitly, without overloading the prompt with internal implementation detail.

The recurring failure pattern in these systems is not “the model is weak.” It is “the prompt asked the wrong thing, in the wrong format, with the wrong context.”

---

## 2. Prompt Philosophy

### 2.1 LLMs should do semantic work
If the task requires open-world judgment, synthesis, disambiguation, confidence framing, contradiction handling, or evidence interpretation, that belongs in the prompt layer. Pushing those decisions into Python heuristics creates brittle systems, silent false negatives, and hard-coded behavior that cannot generalize.

Examples of semantic work that should be LLM-led:
- Selecting which cited URLs are worth fetching next
- Rewriting noisy provider output into usable evidence
- Deciding whether two findings are contradictory, overlapping, or unrelated
- Distinguishing confirmed personnel from tentative personnel
- Determining whether evidence is sufficient to continue a loop

### 2.2 Code should only do exact work
Code is justified only when it:
- Enforces a real invariant
- Performs exact routing or deduplication
- Preserves provenance
- Executes an external side effect
- Guards against structurally impossible states

Prompts should not be weakened just because code could approximate the decision with regexes or token overlap. When in doubt, give the LLM the full context and let it decide.

### 2.3 Prompt quality matters more than schema cleverness
When a weakness can be solved by better prompts, cleaner context, or improved conversation sequencing, prefer that over adding more structure, more fields, or more code-side corrections. Schema exists to support code consequences, not to compensate for avoidable prompt design failures.

---

## 3. Role Design

## 3.1 Roles must describe workflow position, not job titles
A role should tell the model:
- What it is producing
- Who will consume that output
- What the consumer will and will not know
- What kinds of mistakes are dangerous

A role should not just be a label like “research planner” or “provider-native request writer.”

### Bad
- “Lead research planner for an autonomous evidence engine”
- “Provider request writer”
- “Section reviewer”

These are job titles. They tell the model almost nothing operationally useful.

### Good
- “You are writing a self-contained provider brief that will be sent directly to an external research agent. That external agent will not see the plan, task group cards, or internal notes. The brief must stand alone.”
- “You are reviewing one report section against its assigned evidence. Your job is to identify concrete factual, grounding, or citation defects that must be repaired before publication.”
- “You are rewriting a provider result into evidence markdown. Remove bias, strip filler, preserve exact source-backed facts, and separate verified observations from tentative leads.”

## 3.2 Roles must define what is dangerous
The best roles state the failure modes that matter:
- confusing context with evidence
- treating absence as proof
- carrying forward internal IDs or jargon
- overstating confidence
- fabricating URLs or sources
- collapsing distinct entities too early

This should appear in the role or in explicit rule text, not be assumed.

## 3.3 Roles should avoid internal meta-language
Roles should not say things like:
- “Write this so the next model understands it”
- “Your output is not a final section”
- “This will be consumed by another task in the pipeline” unless that fact materially affects content boundaries

When needed, consumer context should be expressed concretely:
- “The next step will only see this markdown and not the raw provider result”
- “The external provider will only receive the brief you write here”

That is operationally meaningful. Abstract pipeline meta-talk is not.

---

## 4. Task Instructions

## 4.1 Task text must describe the deliverable, not the process
Task text should say what must be produced and what constraints govern it. It should not narrate the internal process the system is following.

### Bad
- “Read the evidence, compare the reports, think carefully, and then decide what to do”
- “Analyze the content and figure out the next step”

### Good
- “Produce 3–7 distinct search strategies that attack the task group from materially different angles.”
- “Rewrite the provider result into markdown evidence with exact source-backed observations, explicit uncertainty, and no filler.”
- “Return a structured issue list describing conflicts, entity ambiguities, and verification priorities.”

The more a task sounds like developer instructions instead of an output contract, the worse the results tend to be.

## 4.2 Task text should encode the boundary of responsibility
Every prompt should answer:
- What is in-scope for this call?
- What must be left unresolved?
- What must be forwarded as uncertainty instead of resolved?
- What should be rewritten vs preserved verbatim?

This is especially important in:
- provider request generation
- evidence rewriting
- group synthesis
- section writing
- review and repair tasks

## 4.3 Do not over-compress the deliverable
Prompts that ask for “one short query” or “one concise summary” often force the model to collapse distinct research needs too early. When the architecture wants broad exploration, the prompt must ask for breadth explicitly:
- multiple distinct strategies
- overlapping but non-identical angles
- enough search breadth to surface weak signals and contradictions

This is particularly important for deep research and web research providers.

---

## 5. Rules: How To Encode Failure Modes

## 5.1 Rules should be explicit and named
The strongest prompt systems encode known failure modes as explicit rules rather than relying on general competence.

Examples of effective rule classes and rule patterns from the work:
- `NoBlindNegatives` — do not conclude “nothing found” when evidence is present but unresolved
- `ContextIsNotEvidence` — planning, definitions, and framing documents are not evidence
- `ProviderNarrationIsNotFact` — provider framing text is not automatically trustworthy
- `WebExtractProvenanceOnly` — direct fetch URLs must come from known provenance, not invention
- `DistinctPrimaryFocus` — task groups may overlap, but each must have a distinct primary focus
- `SelfContainedOutput` — external provider briefs must stand alone and contain no internal IDs or task labels

## 5.2 Rules should target real failure patterns
Rules are most valuable when they prevent observed mistakes:
- hallucinated personnel from AI search
- fabricated URLs like `/team`, `/privacy`, `/terms`
- conflating multiple legal entities because a user prompt suggested a hypothesis
- internal task labels like `Q1`, `TG4`, or “candidate comparison record” leaking to external agents
- review tasks missing entity-grounding defects because they only checked style and citations

## 5.3 Rules should be minimal and concrete
The best rules are one or two sentences that describe:
- what to avoid
- what to do instead
- what the consequence of violation is

They should not read like essays or methodology docs.

## 5.4 Rules should be implementation-oriented when code depends on them
If code will act on a structured field, the prompt must make that field reliable enough to drive code behavior. For example:
- `needs_repair` requires explicit issue detection rules
- `should_continue` requires explicit stop/continue rules
- `quality` requires explicit downgrade criteria
- `provider_health` or similar fields require typed justification, not just a Boolean

---

## 6. Context Design

## 6.1 Give the model the full relevant evidence
Large context windows are cheap and valuable. The prompt layer should assume that the model can and should read the full relevant material, not a hand-trimmed excerpt. Manual truncation and Python-side compression create false negatives and brittle reasoning.

That means:
- full provider outputs, not excerpts
- full web page content, not 2,000-character slices
- full evidence sets for a task group, not a few samples
- full cited-source corpora when doing final review or bibliography checks
- full prior round summaries when they materially affect the next decision

## 6.2 Never trim content as a “cost optimization”
Trimming should not be used as a shortcut for better prompt design. When the task is reasoning-heavy, missing context is more damaging than extra tokens.

The following patterns are dangerous:
- passing only the first N evidence packs to section writing
- passing only a handful of evidence samples to final review
- capping prior reports or citations arbitrarily
- reducing provider results to summaries before the rewrite step

## 6.3 Separate evidence from context structurally
The model must be able to tell:
- evidence documents
- planning documents
- identity memo / state documents
- guidance or rule documents
- provider result payloads
- rewritten evidence content

A common failure mode is giving the model both planning assumptions and evidence in indistinguishable form, causing it to cite the plan as if it were factual input.

## 6.4 Give downstream tasks the artifacts they actually need
Many failures came from the right documents not being in context:
- findings categorizers not seeing `EvidencePackDocument`
- section writers not seeing evidence content
- final reviewers missing evidence pack metadata
- metadata extraction tasks not seeing the rewritten markdown they were supposed to classify

Prompt quality cannot compensate for missing required artifacts. Context routing is part of prompt design.

## 6.5 Do not pass entire corpora when scoped evidence exists
“Full context” does not mean “always give everything to every task.” It means give the full relevant context for that task’s purpose.
- section writing should see its assigned evidence, not all sections’ evidence
- group synthesis should see the reports for its group, not every group in every round
- adjudication should see the issue-specific evidence, not the entire scan conversation history

Too much irrelevant context creates bias and missed signals.

---

## 7. Conversation Patterns

## 7.1 Use `categorize -> detailed` for large corpora
When the model must analyze many items, first ask it to categorize or partition them, then run focused detailed passes on each group. This prevents “lost in the middle” failures and improves traceability.

Typical pattern:
1. Categorizer produces structured assignment of items to groups
2. Detailed tasks analyze each group independently
3. Optional synthesis task merges the detailed outputs per group

This is better than asking one model call to summarize everything at once.

## 7.2 Use same-conversation continuation when the second turn depends on the first turn’s interpretation
Good uses:
- missing-output completion loops
- evidence rewrite → metadata extraction
- write → review → repair
- plan → critique → revision

The point is not “chain of thought.” The point is that the second turn should see the first turn’s exact output and refine or classify it.

## 7.3 Use fresh conversations when inherited context would bias judgment
A task should start fresh when it must independently judge output produced earlier.
Examples:
- adversarial plan critique
- conflict adjudication after cross-scan
- entity resolution
- final report review

Reusing the scanner’s or writer’s conversation in those cases can anchor the reviewer to the prior framing.

## 7.4 Do not create hidden multi-turn dependencies
If a task only works because the model knows an unseen wrapper, a Python post-processor, or an implied later step, the prompt contract is broken. Every prompt should be self-sufficient for its intended conversation pattern.

---

## 8. Structured Output vs Markdown

## 8.1 Use markdown for analysis and rich content
Markdown is the default for:
- rewritten provider results
- micro-reports
- group reports
- loop summaries
- section drafts
- final reports
- explanatory memos

Markdown is easier for later LLMs to read than large JSON blobs.

## 8.2 Use structured output only where code needs exact fields
Use structured models for:
- group assignments
- execution items
- issue lists
- review outcomes
- metadata sidecars
- typed handoff signals
- JSON companions

If code is not branching on it, the content usually should not be structured.

## 8.3 Avoid large structured outputs for long-form content
Structured outputs degrade when:
- they exceed a few thousand tokens
- they require many long string fields
- they mix routing fields and long narrative content
- they embed nested JSON-like structures or dictionaries

Observed anti-patterns:
- asking the model to emit provider API payload JSON
- encoding long markdown bodies inside large structured models
- forcing evidence-rich analytical outputs into deep Pydantic trees

## 8.4 Prefer two-step patterns over giant schemas
A highly effective pattern is:
- Turn 1: markdown rewrite or analysis
- Turn 2: small structured metadata extraction from the Turn 1 output

This preserves readability and gives code the fields it needs without forcing the model to do long-form and routing work in the same schema.

---

## 9. Provider-Facing Prompting

## 9.1 External provider briefs must be self-contained
When generating prompts for external agents or search providers, the brief must include everything the external system needs. It must not rely on:
- internal task IDs
- task group labels
- prior pipeline context
- internal analytical vocabulary
- hidden wrappers or suffixes the writer did not know about

This was one of the most damaging failures observed: internal concepts like “candidate comparison record” and `Q1` leaked into provider prompts.

## 9.2 Provider prompts should be tailored to provider capability
A single generic prompt broadcast to all providers is wrong when providers differ meaningfully.

Examples of provider differences captured in the work:
- ChatGPT benefits from a second-pass `additional_prompt`
- Grok tolerates more aggressive broad search and more queries
- Gemini / Perplexity may benefit from explicit multi-query instructions
- Copilot and ChatGPT can be told to proceed immediately and visit relevant websites
- `web_extract` is not search: it is direct page retrieval and later extraction

The writer of the provider brief should know which provider it is targeting and shape the brief accordingly.

## 9.3 Do not make Python silently wrap provider prompts in unknown ways
If provider-facing format or intensity instructions matter, the prompt-writing task should know about them. Hidden Python prefix/suffix wrapping creates mismatches:
- the LLM writes a brief unaware of the final full prompt
- formatting becomes accidental rather than intentional
- debugging becomes harder because the authored prompt differs from the sent prompt

If exact boilerplate must be injected, it should be minimal and justified.

## 9.4 Avoid rigid hardcoded query-count mandates unless they are truly part of the provider contract
Hardcoded counts like “5–10 queries” or “30–50 queries” can be useful in very specific provider-facing wrappers, but as a general prompt design principle they are too rigid. Prefer prompts that ask for enough materially distinct queries or strategies unless the external provider truly benefits from explicit quotas.

---

## 10. Web Fetch And Page Extraction Prompts

## 10.1 Direct fetch should be treated as primary evidence acquisition
Direct web fetch is not just another search result. It is the path to the actual page, often the highest-confidence evidence. Its prompt layer should reflect that.

## 10.2 Page analysis should use the raw page, not a summary
A robust web page analysis pipeline should:
1. fetch the page
2. preserve the full returned content, screenshot, and available attachments
3. run an LLM relevance/extraction step on the raw material
4. rewrite the page into clean evidence markdown
5. extract structured metadata only afterward if needed

The key missing pattern in weaker systems was generating an extraction brief but never actually letting the pipeline LLM use that brief on the raw page.

## 10.3 Use a provider-specific branch for page evidence
`web_extract` should not be processed identically to AI search results.
- web pages need relevance checks
- page extraction should focus on the requested facts
- screenshots may matter for detecting blocked states, paywalls, or dynamic UI content
- page-derived leads should be distinguished from search-provider narrative summaries

---

## 11. Review And Repair Patterns

## 11.1 Review should be structured if code must decide on repair
If code decides whether to launch a repair step, the review output should be structured and include:
- issue list
- repair-needed flag
- explanation or justification
- references to the affected content

## 11.2 Reviews must be consistency-checked
If a structured review says `needs_repair = false` but lists issues, that is a contradiction. The system should reprompt or treat it as invalid. Typed consequences require typed consistency.

## 11.3 Repair should be targeted
Repairs should use:
- the reviewed artifact
- the review output
- the relevant evidence or context
- minimal instructions to fix specific defects

They should not rerun the whole task from scratch unless absolutely necessary.

## 11.4 Final report review must check evidence grounding, not only formatting
Review prompts should explicitly check:
- do named people appear in the evidence?
- are they supported by the right group reports?
- are single-source claims marked as tentative?
- does every cited source resolve to a bibliography entry?
- are unsupported names or entities presented as facts?

Without explicit grounding checks, polished but wrong reports slip through.

---

## 12. Prompt Failures To Avoid

## 12.1 Roles as titles only
Bad:
- “You are a provider planning specialist.”
Good:
- “You are writing provider briefs that will be sent directly to external research agents which do not see the internal plan.”

## 12.2 Process narration instead of deliverable contract
Bad:
- “Analyze the evidence and think carefully.”
Good:
- “Produce 5–10 distinct finding groups. Each group must contain only claims supported by the cited evidence.”

## 12.3 Nested JSON payload generation
Bad:
- Asking the model to emit a stringified API payload inside a structured output field.
Good:
- Ask the model for the plain provider brief; let code place it into the API request.

## 12.4 Internal jargon in external briefs
Bad:
- “Based on the candidate comparison record, decide whether Q1 is resolved.”
Good:
- “Determine whether the company identity is uniquely resolved, ambiguous, or unresolved.”

## 12.5 Blind negatives
Bad:
- “If nothing is found, conclude there is no public evidence.”
Good:
- “If evidence is ambiguous or insufficient, say so explicitly; do not treat partial retrieval or failed providers as proof of absence.”

## 12.6 Context/evidence confusion
Bad:
- Letting definitions, rules, or plan hypotheses appear as evidence inputs.
Good:
- Explicitly instruct that only evidence documents count as factual support.

## 12.7 Hidden provider formatting
Bad:
- Python silently prepends or appends provider instructions the writer never saw.
Good:
- The provider request-writing prompt knows the provider and authors the full provider-facing brief intentionally.

## 12.8 Structured outputs carrying too much prose
Bad:
- giant models containing long markdown bodies and many nested lists
Good:
- markdown first, metadata second

---

## 13. What Good Prompt Systems Look Like

A strong system built on `ai-pipeline-core` tends to have these properties:

- Roles are operational, not decorative
- Tasks specify outputs, not developer process
- Rules encode real failure modes explicitly
- Context includes the full relevant evidence, not arbitrary truncations
- External provider briefs are self-contained and provider-specific
- Long-form outputs are markdown
- Structured outputs are small and used only where code truly branches on them
- Multi-turn chains use continuation when interpretation must carry forward
- Fresh conversations are used where independent judgment matters
- The same evidence is not repeatedly summarized into lower-quality artifacts before analysis

---

## 14. Practical Prompt Design Checklist

Before approving a new PromptSpec, check:

### Role
- Does the role explain what is being produced?
- Does it explain who consumes it and what they do not know?
- Does it state the dangerous failure modes?

### Task
- Is the deliverable explicit?
- Is the task phrased as output requirements rather than process narration?
- Does it define what is in-scope vs unresolved?

### Rules
- Are real failure modes explicit?
- Are negative cases stated clearly?
- Are rules concrete enough to drive behavior?

### Context
- Does the model get all relevant evidence?
- Are evidence and planning/context documents clearly separated?
- Is any necessary artifact missing?

### Output
- Is markdown used for rich analysis?
- Is structured output used only where code needs typed consequences?
- Is the schema small enough to be reliable?

### Conversation pattern
- Should this be a same-conversation follow-up?
- Should this be a fresh conversation?
- Is the continuation or review logic explicit and necessary?

### Provider-facing behavior
- Is the brief self-contained?
- Is provider-specific guidance visible to the writer?
- Are internal IDs, jargon, or hidden wrappers eliminated?

If the answer to any of these is “no,” the problem is usually not the model. It is the prompt design.
