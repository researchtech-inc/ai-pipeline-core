# Observability, Replay, And Debugging Workflows

This document describes how to inspect, download, convert, replay, and analyze runs built on `ai-pipeline-core`. It covers the practical debugging toolchain end-to-end, from raw execution traces to AI-assisted analysis of large runs.

The audience is a senior developer building or operating applications on top of `ai-pipeline-core`, especially systems with many tasks, many conversations, and long-running multi-flow executions.

---

## 1. What The Framework Already Gives You

`ai-pipeline-core` already captures enough raw information to support serious debugging. The challenge is not data collection; it is comprehension.

At runtime, the framework records:

- A **span tree**:
  - deployment
  - flow
  - task
  - attempt
  - conversation
  - llm_round
  - tool_call
- **Documents** with:
  - type
  - name
  - content hash / blob linkage
  - provenance (`derived_from`, `triggered_by`)
- **Conversation state**:
  - messages
  - context documents
  - model options
  - parsed outputs
- **Execution metadata**:
  - status
  - timing
  - retries
  - token usage
  - cost

That gives you replay fidelity. Debugging requires turning that fidelity into an analysis surface a human or AI agent can actually navigate.

---

## 2. The Core Debugging Principle

There are two different jobs:

1. **Replay fidelity**
   - Can the system reconstruct what happened exactly?
2. **Diagnostic comprehension**
   - Can an engineer quickly understand why it happened?

`ai-pipeline-core` is already strong on replay fidelity. Most debugging work therefore focuses on making runs understandable:
- grouping related artifacts
- deduplicating repeated context
- surfacing handoffs between phases
- building document lineage
- slicing traces into context-sized pieces for AI review

---

## 3. Primary Tools

## 3.1 `ai-trace`
Use `ai-trace` to download a finished run into a local snapshot.

Representative usage:

```bash
ai-trace download <run-id> <output-dir>
```

Use this when:
- a run completed and you want a portable offline artifact
- a run failed and you need to inspect what happened before the crash
- you want to hand a snapshot to another engineer or another AI agent

What it gives you:
- a filesystem snapshot of the run
- execution spans
- documents
- blobs
- logs
- LLM call metadata
- top-level run summaries

## 3.2 `ai-replay`
Use `ai-replay` to inspect or re-execute parts of a run.

Representative capabilities mentioned in the framework docs and workflows:
- `ai-replay show`
- `ai-replay run`
- `ai-replay batch`

Example replay target usage:

```bash
ai-replay run --from-db <span-id> --db-path <snapshot-dir> --model <override-model>
```

Use replay when:
- you already know the suspicious span
- you want to reproduce a failure with a different model or options
- you want to isolate a single task or conversation without rerunning the whole pipeline

## 3.3 `.ai-docs`
Before debugging an application deeply, read the framework `.ai-docs` for:
- document model behavior
- prompt compiler behavior
- deployment and flow semantics
- replay and observability conventions

These are not run artifacts, but they are part of the debugging toolchain because they explain what the trace fields mean.

---

## 4. Downloading A Run Correctly

A run may exist in different backends depending on how it was executed.

## 4.1 ClickHouse-backed runs
If the app is configured to persist to ClickHouse, `ai-trace download` can pull the run from the database into a local snapshot directory.

This is the preferred path for production-like runs.

## 4.2 Local/filesystem runs
If the application uses a filesystem database, the run may already exist on disk and can be analyzed directly or downloaded into a structured snapshot.

## 4.3 In-memory / test harness caveat
If a run was executed using an in-memory harness or an ephemeral local runtime, there may be **no persisted trace** even if the run itself produced console output. In that case:
- the background command log may be your only source of truth
- the run may need to be reproduced in a persistent backend before serious analysis

This distinction matters. A failed run with no persisted trace cannot be debugged the same way as a database-backed run.

---

## 5. What A Raw Snapshot Contains

A raw `ai-trace` snapshot typically contains a structure like:

- `summary.md`
- `documents.md`
- `costs.md`
- `llm_calls.jsonl`
- `logs.jsonl`
- `documents/`
- `blobs/`
- `runs/`

### Important files

### `summary.md`
Use this first. It gives the fastest high-level answer to:
- Did the run finish?
- Which flows executed?
- How long did it take?
- How much did it cost?
- Where did it fail?

### `documents.md`
Use this next. It gives a global view of produced documents by type and name.

### `llm_calls.jsonl`
Use this when you need:
- per-call model usage
- token counts
- cost concentration
- volume by task family

### `logs.jsonl`
Use this for:
- warnings
- retries
- runtime anomalies
- non-fatal corruption signals

### `runs/`
This is the detailed execution tree. It is also usually the largest and hardest-to-read part of the snapshot.

### `documents/` + `blobs/`
Together they hold the actual materialized state:
- metadata in `documents/`
- content in `blobs/`

---

## 6. Why Raw Snapshots Become Hard To Use

In large runs, raw snapshots become difficult for three reasons:

1. **Span-centric organization**
   - Great for replay, poor for understanding business progression
2. **Repeated context**
   - Long conversations and repeated prompt prefixes dominate size
3. **Mixed concerns**
   - planning, evidence, synthesis, and audit artifacts are interleaved

In practice, large runs can become impossible to analyze directly in an LLM context window even when the information is all technically present.

This is why conversion and reduction scripts are essential.

---

## 7. Converting Snapshots Into Debuggable Formats

Two conversion patterns emerged as especially useful.

## 7.1 Flow-centric snapshot conversion
Convert the raw snapshot into a structure organized by flow and phase, not by internal span hierarchy.

A useful converted structure looks like:

```text
snapshot-v2/
  manifest.json
  README.md
  plan/
  flows/
    01-planning/
    02-research-round/
    03-research-round-2/
    04-final-synthesis/
  indexes/
  documents/
  debug-contexts/
```

This conversion should:
- keep content once, reference everywhere
- break large outputs into manageable files
- place each flow’s tasks, conversations, and outputs together
- include enough metadata to reconstruct document lineage without opening every blob

### Good file types in a converted snapshot
- `flow.json`
- `tasks.jsonl`
- `conversations-partN.jsonl`
- `outputs-state.json`
- `summary.md`
- `handoff.json`
- `registry.jsonl`
- `indexes/*.jsonl`

### Design targets that worked well
- flow-centric directories
- deterministic ordering
- human-readable JSON/Markdown
- no file above a manageable size threshold
- explicit handoff artifacts between flows

## 7.2 Combined-document view
For some debugging tasks, you want one large, context-ready file that combines:
- document metadata
- associated blob content

A practical format is:

```text
============================================================================
{document JSON}
----------------------------------------------------------------------------
{resolved blob content}
============================================================================
```

This is especially useful when you want to feed the entire analytical corpus into an LLM for:
- final report synthesis
- issue discovery
- post-run intelligence summarization

### Important rule for combined files
The documents must be ordered in a meaningful way.

Chronological filesystem ordering is often wrong or incomplete. Better ordering comes from:
- provenance graph (`derived_from`, `triggered_by`)
- pipeline phase and round
- known producer relationships
- flow/task order as tiebreakers

For this reason, provenance-aware topological ordering is preferred over naive timestamp sorting.

---

## 8. Provenance And Document-Flow Analysis

For serious debugging, you often need more than a snapshot conversion. You need a **document flow graph**.

A useful document-flow tool should generate artifacts like:

- `flow-diagram.md`
- `flow-overview.md`
- `document-catalog.md`
- `task-io.md`
- `lineage/*.md`
- `graph.json`

### What each artifact is for

### `flow-diagram.md`
High-level ASCII or structured view of:
- which flows ran
- which tasks belonged to each flow
- which document types were produced and consumed

Use this first when orienting to an unfamiliar run.

### `flow-overview.md`
Summarize cross-flow handoffs:
- which document types leave one flow
- which later flow consumes them

Use this to detect over-returning flows and missing handoff artifacts.

### `document-catalog.md`
Flat inventory of each document:
- type
- producer
- consumers
- lineage summary

Use this when tracing whether a specific artifact was lost or ignored.

### `task-io.md`
Per-task input/output summary:
- exact documents received
- exact documents returned
- downstream usage

Use this to find:
- wrong task context
- missing inputs
- unused outputs
- over-broad flow outputs

### `lineage/*.md`
Per-document lineage files are especially valuable for:
- group reports
- loop summaries
- conflict resolutions
- entity resolutions
- final report sections
- final assembled report

Use these when asking:
- what created this document?
- what evidence did it depend on?
- what later tasks read it?

### `graph.json`
Machine-readable graph export for:
- custom visualizations
- automated audits
- future scripted analysis

---

## 9. Replay-Driven Debugging

Replay is most effective when you already have a narrow hypothesis.

Typical pattern:

1. Download the full run
2. Convert it or build a document-flow graph
3. Identify the suspicious task/span
4. Replay only that part with:
   - a different model
   - different model options
   - different prompt content
   - the same evidence corpus

Replay is not the first step. It is the step after analysis has already isolated a likely failure point.

### Good replay targets
- a consolidation-like task that produced no findings
- a review task that missed obvious issues
- a provider submission or polling span with missing results
- a final synthesis task that over-pruned evidence
- a companion-generation task that failed schema validation

### Why replay matters
It turns debugging from “read everything” into “test the suspected break point directly.”

---

## 10. Multi-Agent Run Analysis Workflow

For large runs, a single agent rarely sees enough context cleanly. A multi-agent workflow is often the best debugging strategy.

A proven pattern is:

## Phase 1: Download and convert
- download run
- convert snapshot into flow-centric format
- optionally build document-flow graph
- optionally create combined-document files

## Phase 2: Slice by concern
Launch multiple read-only agents with targeted context windows, for example:
- planning analysis
- evidence quality
- findings and reconciliation
- final synthesis
- performance and architecture

## Phase 3: Cross-review
Use `continue_conversations` to make the agents critique each other’s findings.

This is especially effective because:
- one agent often notices logic bugs
- another notices prompt/context issues
- another notices architectural misuse of framework primitives

## Phase 4: Focused follow-up rounds
If a particular theme emerges, launch deeper targeted rounds for:
- citation integrity
- document routing
- prompt misuse
- flow over-returning
- runtime principle violations
- provider behavior
- cross-round evidence loss

## Phase 5: Synthesis
After enough focused rounds, run a final synthesis pass that:
- deduplicates findings
- separates fixed vs unresolved issues
- groups them by severity and type

### Important practice
Use **read-only, no-tools** analysis agents for diagnosis first. Only move to edit mode after the issue surface is clear.

---

## 11. Context Budgeting For AI Analysis

Large runs require explicit context budgeting.

### Practical pattern
Each analysis agent should receive:
- only the files needed for its phase or issue
- enough source code to map findings back to implementation
- only the most relevant snapshot slices

### Good context slices
- planning flow + planning docs
- one research round + its evidence docs
- final synthesis + final outputs
- task-io + lineage for provenance debugging
- proposal/spec docs only when comparing intent vs implementation

### Bad context slices
- full run snapshot
- every source file
- every report from every prior review round
- large raw provider result corpora unless the task truly needs them

### Why this matters
When context is too large:
- the model reads less carefully
- findings become shallower
- identical evidence gets re-explained instead of analyzed

Good debugging is not just about having the data. It is about giving each analysis step the smallest correct slice.

---

## 12. Debugging Patterns That Work Well

## 12.1 Flow-by-flow analysis
Use when:
- you want to know which stage first went wrong
- the run completed but produced poor results
- you suspect the failure is local to one phase

This works best with:
- flow-centric snapshots
- one agent per flow
- a later cross-review pass

## 12.2 Evidence lineage analysis
Use when:
- real evidence was found but never appeared in the final report
- the report cites the wrong thing
- findings seem to vanish between stages

This requires:
- document catalog
- lineage files
- task input/output maps

## 12.3 Prompt/context debugging
Use when:
- LLM outputs say “no evidence found” despite evidence being present
- providers receive malformed or over-internalized tasks
- review passes fail to notice obvious mistakes

This requires:
- exact PromptSpec definitions
- task wiring
- task input document lists
- conversation records

## 12.4 Provider pipeline debugging
Use when:
- providers return weak or malformed results
- citations disappear
- wrong URLs are fetched
- high failure rate appears in one provider family

This requires:
- gateway code
- request-generation code
- provider-specific prompt formatting
- raw provider result documents

## 12.5 Synthesis debugging
Use when:
- the final report is thinner than the evidence corpus
- bibliography is incomplete
- sections contradict earlier documents
- final audit failed to catch visible issues

This requires:
- report plan
- section drafts
- section reviews
- repair documents
- final report and companion
- cited evidence links

---

## 13. Common Failure Modes This Workflow Exposes Well

These workflows repeatedly expose the same classes of problems:

- Evidence existed but was not passed into the next task
- Flow returned too many intermediate artifacts or too few handoff documents
- PromptSpec declared documents but the task did not pass them
- Provider results were parsed incorrectly at the integration boundary
- Runtime provenance was broken by synthetic or missing parents
- A review task saw summaries instead of raw evidence
- A synthesis task used too broad or too narrow evidence context
- A script combined documents in the wrong order and made downstream analysis misleading

The point of the debugging system is not only to catch bugs. It is to make these classes of bugs obvious enough that they can be categorized and fixed systematically.

---

## 14. Recommended Framework Enhancements For Better Debugging

Several improvements repeatedly proved valuable enough that they should be treated as first-class framework features.

## 14.1 Task completion summaries
The framework should auto-generate compact task summaries containing:
- input doc counts by type
- output doc counts by type
- conversation count
- cost
- duration
- retry count

This is framework-automatable and dramatically reduces the need to read raw conversations first.

## 14.2 Flow business checkpoints
Applications should emit a compact checkpoint after each flow that summarizes:
- current belief state
- key unresolved questions
- open conflicts
- coverage or loop decisions

The framework can support this pattern, but the application must define the semantic contents.

## 14.3 Document delta generation
For each flow, the framework should be able to compute:
- new documents
- changed documents
- removed documents

This makes cross-flow progression inspectable without manual state diffs.

## 14.4 Replay target maps
The download/conversion tool should generate replay target indexes by:
- task class
- purpose
- span id
- document lineage

This closes the gap between “we found the suspicious thing” and “now replay just that thing.”

## 14.5 Phase segmentation via operation spans
Applications should place explicit phase boundaries using traced operations so downloads can segment flows into:
- evidence gathering
- consolidation-like analysis
- cross-issue resolution
- finalization

This allows AI analysis to load only the relevant phase slice.

## 14.6 Document role namespacing in prompt compiler
The framework should make it easier to distinguish:
- evidence documents
- interpretation context
- control/handoff documents

This helps both correctness and debuggability because the LLM sees structurally different kinds of context.

## 14.7 Snapshot-v2 style output built into `ai-trace`
The converted, flow-centric debug format should be a built-in output mode, not an ad hoc application script.

---

## 15. When To Use Which Artifact

Use this quick map:

- **Need high-level run status?**
  - `summary.md`
- **Need document inventory?**
  - `documents.md`
- **Need cost concentration?**
  - `llm_calls.jsonl`, `costs.md`
- **Need warnings and retries?**
  - `logs.jsonl`
- **Need exact task/doc handoff?**
  - `task-io.md`
- **Need provenance of one document?**
  - `lineage/<doc>.md`
- **Need a context-ready corpus for AI synthesis?**
  - combined-document file
- **Need to re-execute a suspicious step?**
  - `ai-replay`
- **Need to understand a large run structurally?**
  - converted flow-centric snapshot + document-flow graph

---

## 16. Operational Guidance

### Do
- download every important failed run before making fixes
- build a converted snapshot before doing broad AI analysis
- use provenance and task I/O before reading large raw conversations
- split analysis by phase or issue
- cross-review analysis outputs before moving to implementation
- keep debugging artifacts in a structured temporary tree

### Do not
- start with raw `runs/` traversal for a large run
- dump the whole snapshot into one analysis context
- implement fixes before the issue surface is stabilized
- trust a single-agent diagnosis for large, multi-flow failures
- confuse replay fidelity with run comprehensibility

---

## 17. Minimal End-To-End Debug Workflow

For a serious failed run, the recommended sequence is:

```text
1. Download the run
2. Read summary.md and documents.md
3. Convert snapshot to flow-centric format
4. Build document-flow graph
5. Create combined-document views for evidence-rich analysis
6. Launch phase-specific read-only analysis agents
7. Cross-review findings
8. Replay only the confirmed suspicious spans
9. Implement fixes
10. Re-run and compare
```

That sequence is what turns observability data into an actual debugging workflow.

---

## 18. Final Principle

The framework already captures enough information. The debugging problem is almost never “we do not have the data.”

The real problem is usually one of these:
- the data is organized for machines, not for investigators
- the wrong slice is being inspected
- provenance is hard to follow
- the evidence is duplicated instead of summarized
- the analysis is not staged

Good observability on `ai-pipeline-core` therefore means two things at once:
- preserve exact replayable truth
- create derived, flow-aware, provenance-aware analysis surfaces that humans and AI agents can actually reason over

That is the standard this toolchain should meet.