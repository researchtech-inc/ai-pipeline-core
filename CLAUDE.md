# AI Pipeline Core — Coding Standards & Rules

> Rules and standards for the ai-pipeline-core framework repository. Governs how framework code must be written, not what the framework provides (see `.ai-docs/` for API documentation).

## Design Principles

1. **Framework Absorbs Complexity, Apps Stay Simple** — All heavy/complex logic lives in the framework. Application code built on this framework should be minimal and straightforward. Execution tracking, retries, deployment, progress tracking, storage, logging, and validation are handled automatically.

2. **Deploy First, Optimize Later** — Get working system fast. Architecture must allow future optimization without major refactoring.

3. **Distributed by Default** — Multiple processes on independent machines with centralized services (LiteLLM, ClickHouse, logging). Design to avoid race conditions and data duplication.

4. **AI-Native Development** — Designed for AI coding agents to understand, modify, and debug. One correct way to do everything. Definition-time validation catches mistakes before runtime.

5. **Single Source of Truth** — No duplicate documentation. Code defines behavior. Auto-generate documentation from code.

6. **Self-Explanatory Code** — Code must be understandable without deep-diving into documentation or framework source code. Naming, structure, and types make intent obvious.

7. **Automate Everything Possible** — If a check, validation, or transformation can be automated, it must be. Manual steps invite errors.

8. **Minimal Code** — Less code is better code. Every line must justify its existence.

9. **No Legacy Code** — No backward compatibility layers, deprecation shims, or references to previous implementations. Unused code must be removed immediately.

10. **No Unvalidatable Derivatives** — When a value is derived from a typed source (field name, class name, enum variant), it must be computed programmatically, not written as a manual string. Dict keys mirroring model fields, string identifiers mirroring class names — if the type checker can't trace it back to the source, derive it from the typed source instead. This prevents silent breakage when renaming.

11. **Sequential phases, not if/elif branches** — When logic is "try A, then fall back to B if A is insufficient," write it as two sequential blocks with a condition between them — not `if A: ... elif B: ...` which duplicates the B logic and obscures the relationship.

12. **Poka-Yoke (Mistake-Proofing)** — The framework follows the Poka-Yoke methodology: make mistakes impossible rather than relying on vigilance to avoid them. Prevention over detection, detection over correction. Every class validates its own constraints at definition/import time via `__init_subclass__`. Construction paths enforce correct provenance (four factory methods, no raw constructors). Frozen models eliminate mutation bugs structurally. Layered static analysis (ruff, basedpyright, semgrep, vulture, interrogate) catches what types alone cannot. When a bug is found, the response is to close the entire category — add a structural guard, not just a point fix (§3.2 Bug Response Protocol). Actionable error messages (§4.13) complete the loop: every failure tells the caller exactly how to fix it, enabling AI agents to self-correct without external documentation.

---

## 1. Architecture Rules

### 1.1 Async Execution

All operations must be asynchronous. No blocking I/O calls allowed.

**`async def` must contain async operations** — Functions declared with `async def` must contain at least one `await`, `async for`, or `async with` statement. Functions without async operations must not be marked `async`. Enforced via semgrep rule.

**Exceptions:**
- Protocol stubs (method signature only)
- ABC base class methods meant for override
- In-memory test implementations (e.g., MemoryDatabase)

### 1.2 Immutability & Safety

- **No mutable global state that creates inter-task dependencies.** Module-level variables fall into three permitted categories:

  **Category 1 — Constants and frozen configuration.** `settings = Settings()`, frozen mappings, `frozenset` constants, module-level type aliases. Always allowed.

  **Category 2 — Infrastructure singletons.** HTTP client pools, provider facades, rate limiters. Allowed when ALL of the following hold:

  - The variable is assigned exactly once at module scope and never reassigned.
  - Internal state (connections, caches, locks) is a private implementation detail that never leaks to callers through the public API.
  - The singleton is **caller-stateless**: calling `await provider.fetch(url)` produces the same result regardless of what other tasks, flows, or deployments have previously called on the singleton. No task writes state that another task reads through the singleton.
  - The singleton exposes an `override()` context manager for test replacement, backed by a ContextVar for per-test isolation.
  - The variable is annotated with a `# infrastructure singleton` comment at the assignment site.

  **Category 3 — Import-time registries.** Class registries populated by `__init_subclass__` hooks during module import. Allowed with `# nosemgrep` plus a justification comment, and a `reset_registries()` function for test isolation.

  **Everything else is forbidden.** If Task A must run before Task B for B to produce correct output, that dependency must flow through Documents, function arguments, database state, or an external service — never through a module-level variable.

  **The litmus test:** Delete all calls to the global from Task A. Run Task B in isolation. Does Task B still produce correct output? If yes, the global is infrastructure (category 2). If no, it carries business state and must be replaced with a Document.
- Default timeouts on all operations — nothing hangs indefinitely
- Strict type checking throughout
- Pydantic models use `frozen=True` where possible
- Dataclasses use `frozen=True` and `slots=True`
- **Module-scoped replayable classes** — `BaseModel`, `BaseSettings`, `Document`, `Tool`, `Conversation`, `PipelineTask`, `PipelineFlow`, `PipelineDeployment`, `FlowOptions`, `DeploymentResult`, and `response_format` models must be defined at module scope. Function-local classes get `<locals>` in their `__qualname__`, breaking codec/replay. Enforced via semgrep

### 1.3 Module System

- Modules/agents/tools form **acyclic dependency graph**
- Clear module boundaries with defined inputs/outputs
- Task inputs use named typed parameters: scalars, enums, frozen BaseModels, `Conversation`, `Document` subclasses, and typed containers thereof
- Flow inputs use named `Document`-typed parameters resolved from the deployment blackboard
- **Context as document types** — Prompt specs declare `input_documents` for expected context; missing documents warned at runtime

### 1.5 Document Philosophy

Documents are durable pipeline artifacts with independent meaning.

- Make something a `Document` when a reader investigating the run would care about the artifact on its own.
- Use frozen models or dataclasses for transient transport values, prompt scaffolding, lookup tables, or routing glue with no durable meaning.
- If an artifact exists only to shuttle a field or two between steps, it is probably not a `Document`.

### 1.6 Task Atomicity

Tasks are atomic in **purpose**, not necessarily in wall-clock duration.

- A task may perform multiple internal operations when they are one coherent business action with one execution record.
- Tasks own model calls, tool use, provider calls, and durable artifact creation.
- Tasks must not orchestrate other tasks. If one unit of work depends on another, the flow must coordinate them.

### 1.4 Configuration

- System-level config via `Settings` base class (Pydantic BaseSettings)
- Module-level overrides when needed
- No config duplication — define once, reuse everywhere
- Model configuration includes model name AND model-specific options (e.g., `reasoning_effort`)
- **Retry resolution hierarchy** — Retries on tasks, flows, and conversations default to `None` at the class level. At runtime they resolve via: class-level override → deployment-level override → `Settings` fallback (env-configurable: `TASK_RETRIES`, `FLOW_RETRIES`, `CONVERSATION_RETRIES` and corresponding `*_DELAY_SECONDS`). Settings defaults: task/flow retries `0`, conversation retries `2`

---

## 2. LLM Implementation Rules

These rules govern how LLM-related code must be written.

### 2.1 Token Economics — Input Tokens Are Cheap

**Core principle:** Input tokens are cheap; cached input tokens are near-free. Never sacrifice accuracy or context quality to reduce input size. Full context improves accuracy and is cheap.

- Support large contexts (100K-250K tokens)
- **Never trim or summarize inputs** to "save tokens"
- Implement prefix-based caching with configurable TTL
- Prefer sending identical large prefixes across calls over sending tailored smaller prompts per call

### 2.2 Preparation-First Execution

Because cache lives at most 5 minutes:

1. **Fetch phase**: Gather all external data (web content, screenshots, API responses)
2. **Execution phase**: Fire all LLM calls with shared context prefix

**Anti-pattern**: Interleaving slow I/O with LLM calls causes cache misses because later calls may arrive after cache TTL expires.

### 2.3 No Batching

Do not batch multiple items into a single LLM call to "save tokens." With caching:
- Separate calls are nearly as cheap as batched calls (shared prefix is cached)
- Separate calls produce higher accuracy (LLM focuses on one task)
- Separate calls are easier to implement, retry, and debug
- Separate calls return structured output per item without complex parsing

### 2.4 Image Handling

Maximum image resolution is 3000x3000 pixels. The framework handles per-model downscaling internally via `ImagePreset`.

**Image Processing Pipeline:**
1. **Load and normalize** — EXIF orientation fix (important for mobile photos)
2. **If within model limits** — Send in original format as single image
3. **If taller than limit** — Split vertically into tiles with **20% overlap**, each tile within height limit
4. **If wider than limit** — Trim width (left-aligned crop). Web content is left-aligned, so right-side content is typically less important.
5. **Describe the split in text prompt** — "Screenshot was split into N sequential parts with overlap"

### 2.5 Model Cost Tiers

- **Expensive** (pro/flagship): gemini-3.1-pro, gpt-5.4. Use for complex reasoning, final synthesis.
- **Cheap** (flash/fast): gemini-3-flash, grok-4.1-fast. Use for high-volume tasks, formatting, conversion, structured output extraction.
- **Too small** (nano/lite): Insufficient for production pipeline tasks. Do not use.

### 2.6 Model Reference Preservation

**Do not remove model references that appear unfamiliar.** Models are released frequently and the codebase may reference models that are newer than the AI coding agent's training data. If a model name exists in code, assume it is valid unless there is concrete evidence otherwise (e.g., provider returns "model not found" error).

### 2.7 Structured Output

Structured LLM output rules:
- Send schema to LLM automatically via `response_format` (never explain JSON structure in prompts)
- Parse and validate response against Pydantic model

**Quality limits**:
- Structured outputs degrade beyond ~2-3K tokens
- Nesting beyond 2 levels causes quality degradation
- `dict` types are not supported in structured output or `Tool.Input` schemas — use lists of typed models. Enforced at import time for tools (OpenAI strict mode incompatible)
- Field names `strict` and `additionalProperties` are reserved in `Tool.Input` (collide with LiteLLM key stripping)
- Complex structures should be split across multiple calls

**Decomposition Fields Before Decision Fields:**
In BaseModel definitions, fields that decompose the problem into concrete dimensions must be defined before fields that represent conclusions. LLMs generate tokens sequentially — if the decision field comes first, the LLM commits to a conclusion and then rationalizes it.

```python
# WRONG — decision before analysis
class VerificationResult(BaseModel):
    is_valid: bool
    summary: str


# WRONG — generic scratchpad
class VerificationResult(BaseModel):
    reasoning: str  # Just "think step by step" in a field
    is_valid: bool


# CORRECT — domain-specific decomposition leads to decision
class VerificationResult(BaseModel):
    source_content_summary: str  # What the source says
    report_claims: str  # What the report claims
    discrepancies: str  # Differences found
    assessment: str  # Reasoned conclusion
    is_valid: bool  # Decision follows from decomposition
```

### 2.8 Document XML Wrapping

When documents are added to LLM context, the framework wraps them in XML. This boundary separates data from instructions — the **prompt injection defense**.

**All structured data for LLM context must be wrapped in a Document.** Never construct XML manually (e.g., f-string `<document>` tags). The framework handles escaping, ID generation, and consistent formatting.

### 2.9 Thinking Models

All LLMs (2026) perform internal reasoning. The framework must NOT add:
- Chain-of-thought prompting
- "Think step by step" instructions
- Scratchpad patterns

These are redundant and can interfere with the model's native reasoning. Reasoning effort is controlled via `ModelOptions.reasoning_effort` where supported.

### 2.10 Long Response Handling

LLMs produce quality degradation in responses longer than 3-5K tokens. Use:
- Conversational follow-up patterns for building long outputs incrementally
- Multi-turn exchanges where each follow-up receives previous responses as conversation history

Tasks requiring long outputs should not use a single call requesting a large response.

### 2.11 Additional LLM Anti-Patterns

Beyond the rules above (no batching §2.3, no CoT §2.9, no input trimming §2.1):

| Anti-Pattern | Why It's Wrong |
|--------------|----------------|
| Generic `reasoning: str` scratchpad fields | Redundant with model's native reasoning; use domain-specific decomposition fields (§2.7) |
| Numeric confidence scores without criteria | Each call interprets scale differently; hallucinated results |
| Explaining JSON structure in prompts | Redundant with schema sent via `response_format`; degrades quality |

---

## 3. Code Quality Standards

### 3.1 Type Safety

- Complete type hints on all functions and return values
- Pydantic models for all data structures
- Use specific types: `UUID` not `str` for identifiers, `Path` not `str` for file paths
- Constrained strings must be custom types (NewType, Annotated, or wrapper class)
- Definition-time validation via `__init_subclass__` where applicable

### 3.2 Testing

- Tests serve as usage examples
- Test mode allows running with cheaper/faster models (simple model swap via config)
- Individual modules must be testable in isolation
- Framework provides test harness utilities requiring zero configuration

**Bug Response Protocol:**
When a bug is found or reported, do not jump to fixing it. Follow this order:

1. **Investigate why it wasn't caught.** Why did existing linters, semgrep rules, type checks, and tests miss this? If a tool *should* have caught it, fix the tool configuration or add a rule so it catches this class of bug going forward.
2. **Prevent, don't just fix.** The best fix is making the bug impossible. If an architecture change, a definition-time validation (`__init_subclass__`), a semgrep rule, or a type constraint can prevent this entire class of bug from ever occurring again, do that — even if it requires redesign. A one-line fix that leaves the door open for recurrence is inferior to a structural change that closes it.
3. **Find all similar instances.** Before fixing the specific bug, search the codebase for the same pattern. If one place has this bug, others likely do too. Fix them all at once.
4. **Prove with a test.** Write a failing test that asserts the **correct** behavior. Mark it `@pytest.mark.xfail(reason="...", strict=True)` so it proves the bug exists. Only then implement the fix. After fixing, remove the `xfail` marker — the test becomes a permanent regression guard.

This is the purpose of the framework's extensive tooling (ruff, basedpyright, semgrep, vulture, interrogate, definition-time validation): prevent bugs from happening, or detect them before runtime. Every bug that reaches production is a signal that the prevention layer has a gap — close the gap, not just the bug.

**No Blind Suppression of Tooling Warnings:**
When linters, type checkers, semgrep, tests, or CI/CD checks report an issue, investigate it fully and fix the root cause. Do not suppress warnings without justification — no bare `# noqa`, `# type: ignore`, `# nosemgrep`, `pytest.skip()`, `xfail` (except for TDD bug proving above), disabling rules, commenting out code, deleting the check, or any other form of suppression. These tools detect real coding problems — silencing them hides bugs instead of fixing them. If a warning is genuinely a false positive or structurally unavoidable (e.g., imports after `warnings.filterwarnings`), add the narrowest possible suppression (single line, specific rule code) with a comment explaining why.

### 3.3 AI-Focused Documentation

The framework auto-generates documentation for AI coding agents via `tools/docs-generator/` (separate workspace package).
Use the `ai-docs` CLI command:

```bash
ai-docs generate   # Regenerate .ai-docs/ from source code
ai-docs check      # Validate completeness, size limits, and private reexports
```

`make docs-ai-build` / `make docs-ai-check` delegate to `ai-docs`. The pre-commit hook and CI run `ai-docs generate` and fail if `.ai-docs/` becomes stale.

Guide properties:
- Public/private determined by `_` prefix convention
- Full source code with comments included
- Examples extracted and scored from test suite
- 40KB warning threshold per guide, 45KB hard limit for README.md
- CI-enforced freshness

**Visibility by Naming Convention:**
- No `_` prefix → public (included in docs)
- Single `_` prefix → private (excluded)
- Dunder methods (`__init__`, `__eq__`, etc.) → always public
- Files starting with `_` (e.g., `_helpers.py`) → private modules (excluded entirely)
- Exception: `__init__.py` is always processed

**Docstring Rules:**
- No `Example:` blocks — tests serve as examples
- Inline comments within method bodies are preserved

**Test Marking:**
- `@pytest.mark.ai_docs` — Explicitly include a test as an example
- Marked tests get priority — included first regardless of score
- Auto-selected tests are scored by: symbol overlap (high bonus), test length (shorter preferred), mock usage (penalty)
- Error examples using `pytest.raises` included in ERROR EXAMPLES section

**Internal Types:**
- Private classes matching `_CapitalizedName` pattern that appear in public API signatures are automatically included in INTERNAL TYPES section
- Ensures guides are self-contained

**Guide Structure Rules:**
- Every guide includes `## Imports` with two-tier import paths: `from ai_pipeline_core import ...` (top-level) and `from ai_pipeline_core.<module> import ...` (sub-package symbols not at top level)
- Module-level `NewType`, `type` aliases, and public `UPPER_CASE` constants must be extracted into `## Types & Constants` section
- `.ai-docs/README.md` (generated) includes comprehensive per-module API summaries with all public symbols
- When `__init_subclass__` calls private helpers, the class docstring must enumerate all constraints as rule lines
- Prefer `class MyType(str)` over `NewType` for types that should appear in documentation with their own docstring
- Protocol and Enum classes are tagged with a comment line (`# Protocol` / `# Enum`) above the class definition

### 3.4 Module Cohesion

Each framework module produces one AI-docs guide. That guide must be **self-sufficient for usage**: an AI coding agent must be able to correctly use the module's public API by reading only that guide.

**The acid test**: "Can an AI agent correctly use this module by reading only its guide?" If using module A requires reading module B's guide, the module boundaries must be redrawn.

- **One concern, one module** — Related functionality lives in a single module directory
- **Public API self-documentation** — Parameters triggering behavior in other modules must be documented on the public API
- **Imports allowed, knowledge dependencies forbidden** — Module A may import from B internally, but using A's public API must not require reading B's documentation

---

## 4. Code Hygiene Rules

### 4.1 Protocol and Implementation Separation

Protocol definitions must not be mixed with implementations in the same file. When a Protocol is needed, place it in a separate `_protocols.py` or `_types.py` module.

**Exceptions:** Files explicitly excluded in semgrep config (protocol.py, base.py, _types.py).

### 4.2 No Patch Reference Comments

Comments referencing bug fixes (`# FIX 1:`, `# Fixes #123`, `# Fixes issue`, `# Patch for...`, `# Workaround for...`) are forbidden. Code must be self-explanatory. Bug fixes are documented by regression tests.

### 4.3 Magic Number Constants

Numeric literals used as thresholds, limits, or configuration values must be defined as module-level or class-level constants with descriptive names.

**Exceptions:** `0`, `1`, `-1`, `2`, and standard mathematical constants.

```python
# Wrong
if len(url) < 40:
    return url

# Correct
MIN_URL_LENGTH_FOR_SHORTENING = 40
```

### 4.4 Silent Exception Handling

`except Exception: pass` and `except: pass` are forbidden. Caught exceptions must be:
1. Logged with context, OR
2. Re-raised (possibly wrapped), OR
3. Converted to a specific return value with a comment explaining why swallowing is safe

### 4.5 File Size Limits

- **Warning:** Files exceeding 500 lines (excluding blanks and comments)
- **Error:** Files exceeding 1000 lines

**Suggested splits:**
- Types/protocols → `_types.py` or `_protocols.py`
- Pure functions/utilities → `_utils.py`
- Constants/patterns → `_constants.py`

### 4.6 Export Discipline

Every module with public symbols must define `__all__` listing its public API. Internal modules must be prefixed with `_` (e.g., `_helpers.py`).

### 4.7 Module Naming

Module and directory names must describe the domain problem, not implementation technique.

**Anti-pattern:** `content_protection/` (describes technique)
**Correct:** `token_reduction/` or `url_shortener/` (describes purpose)

### 4.8 Algorithm Complexity

Operations on unbounded input should prefer O(n) or O(n log n). O(n²) is acceptable only when:
- Input size has a known small bound (e.g., n ≤ 100), AND
- The simpler algorithm reduces code complexity

Document size assumptions when using higher-complexity algorithms.

### 4.9 Duplicate Logic

Functions or match/case blocks with >80% structural similarity must be consolidated. Use parameterization, helper functions, or lookup tables.

### 4.10 Document Construction Paths

Four factory methods, each with strict provenance semantics:
- `create_root(reason=...)` — pipeline inputs with no provenance
- `derive(derived_from=...)` — content transformations (summaries, analyses)
- `create(triggered_by=...)` — causally triggered documents
- `create_external(from_sources=...)` — URI-based provenance (URLs, MCP)

All constructors accepting document provenance take `Sequence[Document]` objects (not SHA256 strings). Direct `Document(...)` construction is forbidden outside framework internals and tests (enforced via semgrep).

### 4.11 File-Level Isolation (Application Code)

Enforced at import time via `__init_subclass__` in `_file_rules.py`. Framework internals, tests, and examples are exempt.

- **One `PipelineFlow` per file** — no mixing with tasks or specs
- **One `PipelineTask` per file** — no mixing with flows or specs
- **`PromptSpec` co-location** — at most one standalone spec per file; follow-up specs (`follows=`) targeting the same file are allowed; cross-file follow-ups must be alone in their file
- **Mandatory docstrings** — application flows, tasks, and specs must have non-empty docstrings
- **`_abstract_task = True` / `_abstract_flow = True`** — set on intermediate base classes to skip `run()` validation. Concrete subclasses are validated normally
- Single-document task returns are allowed; the runtime normalizes them to a tuple internally.

### 4.12 Task Return Annotations

`PipelineTask.run()` return type must be `MyDocument`, `tuple[MyDocument, ...]`, `None`, `list[MyDocument]`, or unions thereof. Single-document return is allowed; the runtime normalizes it to a tuple. Enforced in `_type_validation.py`.

### 4.13 Actionable Error and Warning Messages

Warning and error messages must include not only what went wrong, but also how to fix it and how to do it correctly. The reader (often an AI coding agent) should be able to resolve the issue from the message alone without consulting documentation.

```python
# Wrong — states the problem but not the solution
logger.warning("Field '%s' value is too long (%d chars).", field_name, len(value))

# Correct — states the problem, the correct usage, and how to fix it
logger.warning(
    "PromptSpec '%s' field '%s' has a long or multiline value (%d chars). "
    "Field parameters are for short, single-line values (up to %d chars). "
    "Pass longer content as a Document via input_documents and send_spec(documents=[...]).",
    spec_name,
    field_name,
    len(value),
    MAX_LENGTH,
)
```

### 4.14 Runtime Guards

- **Task-in-task detection** — Tasks must not call other tasks. A `RuntimeError` is raised if `Task.run()` is called from within another task's execution scope. Orchestration belongs in flows.
- **Conversation-in-flow detection** — `Conversation.send()` and related send paths must not be called directly from a flow. A `RuntimeError` is raised if an LLM call is made from flow scope without a task.
- **Document type freezing** — `input_document_types` and `output_document_types` on flows and tasks are frozen tuples after class definition. They cannot be reassigned.

### 4.15 Return Discipline

- Tasks must not return input documents unchanged. The deployment blackboard carries earlier artifacts automatically, so tasks do not need to forward them.
- Returning an input document with the same SHA256 from a task raises `TypeError`. Use `derive()`, `create()`, `create_external()`, or `create_root()` to create the correct new artifact.
- Flows returning unchanged inputs are treated as a warning-level smell. The Great Filter expects flows to return only the phase handoff, not cargo-forward earlier artifacts.

### 4.16 Fan-Out Warning

`collect_tasks()` warns when more than 50 handles are collected. Set `max_fan_out` on the flow class to document intentional high fan-out; this is documentation today and reserved for future enforcement.

---

## 5. Deployment & Operations

### 5.1 Deployment Safety

- New deployments must not break running workflows
- Running processes finish on old version
- New requests use new version
- Graceful version transition

### 5.2 Scalability

- Horizontal scaling via additional workers
- Centralized services handle coordination (LiteLLM, ClickHouse)
- No single points of failure (where possible)
- Deployment system should be able to manage resources (API keys, models, scaling)

### 5.3 Deployment Plan

Deployments define the maximum execution path via `build_plan()` returning a `DeploymentPlan` with `FlowStep` entries.

- `FlowStep` wraps one flow instance and may add `run_if=FieldGate(...)` plus an optional `group` tag.
- `FieldGate` reads a named field on the latest control document of a specified type and applies a truthy/falsy or equality check.
- `group_stop_if` on `DeploymentPlan` stops all remaining steps in a group when a control document satisfies the stop gate.
- `build_flows()` remains a fallback wrapper, but the plan model is `build_plan()` / `DeploymentPlan` / `FlowStep` / `FieldGate`.

### 5.4 The Great Filter

Flows are the great filter of pipeline state.

- Tasks produce artifacts during the phase.
- The framework preserves those artifacts in the durable record.
- The flow returns only the handoff artifacts that the next phase actually needs.

The deployment accumulates those handoffs in a blackboard. Earlier artifacts remain available automatically, so flows must not forward unchanged inputs just to keep them alive.

### 5.5 Composite Documents

Use a composite document when several artifacts always travel together across flow boundaries and form one coherent handoff.

- Composite documents keep flow signatures narrow and readable.
- Do not bundle unrelated artifacts just for convenience.
- Composite handoffs are typically assembled by a dedicated task at the end of a phase.

### 5.6 Control Documents

Control documents are small typed documents used for runtime gating.

- A control document records a durable, inspectable decision with explanatory content.
- A `FieldGate` reads a named field on the latest control document of the specified type.
- A control document that is only a flag wrapper with no explanatory content is a design smell.

---

## 6. Decisions Made

| Decision | Choice | Notes |
|----------|--------|-------|
| Orchestrator | Prefect | Flow/task orchestration, state management |
| LLM Proxy | LiteLLM (primary), OpenRouter (compatible) | Unified multi-provider access |
| Database | ClickHouse (production), filesystem (CLI/replay), in-memory (testing) | Unified storage: `spans`, `documents`, `blobs`, `logs`. Content-addressed with SHA256 deduplication |

---

## 7. Out of Scope

- Compliance/regulatory features (GDPR, SOC2)
- Multi-tenant isolation
- Complex access control/RBAC
- Custom orchestrator implementation

---

## 8. Dev CLI — Test, Lint, and Check Workflows

**Use the `dev` CLI for all test, lint, and type-check operations.** Do not run `pytest`, `ruff`, or `basedpyright` directly — a Claude Code hook will block these commands with an actionable message pointing to the correct `dev` command.

### First thing: run `dev info`

Before running any tests or checks, **always start with `dev info`**. It prints the full usage guide, auto-detected check pipeline, available test scopes, runner configuration, and infrastructure status. Read its output — it tells you everything you need to know about the project's dev workflow.

### Commands

```bash
# Testing
dev test                # Run affected tests (auto-scoped from git changes, uses testmon)
dev test pipeline       # Run tests for a specific module
dev test --lf           # Rerun only last-failed tests
dev test --full         # Full suite in parallel (before commit)
dev test --available    # Include infrastructure tests (auto-detects Docker/API keys)
dev test --coverage    # Full suite with code coverage (80% threshold from pyproject.toml)
dev test --coverage pipeline  # Coverage for pipeline tests (threshold not enforced)
dev test --force        # Force rerun even if no files changed (rarely needed)

# Code quality
dev format              # Auto-fix lint + formatting (ruff format + ruff check --fix)
dev lint                # Check lint without fixing
dev typecheck           # Type checking (basedpyright)

# All checks in order (lint → typecheck → deadcode → semgrep → docstrings → tests)
dev check               # Full validation pipeline
dev check --fast        # Lint + typecheck only

# Utilities
dev status              # Changed files, last run results, suggested next command
dev info                # Usage guide + auto-detected config + infrastructure status
```

### Timeout

`dev test --full`, `dev test --available`, `dev test --coverage`, and `dev check` can take up to 10 minutes. Use a 10-minute timeout (600s) when running these commands via the Bash tool.

### Workflow

1. Write code
2. `dev format` — auto-fix lint/formatting
3. `dev check --fast` — verify lint + types pass
4. `dev test` — run affected tests
5. If tests fail: fix code, then `dev test --lf`
6. `dev check` — full validation before commit

### Testmon and `--force`

`dev test` uses testmon by default — only tests whose dependencies changed actually run. The summary shows e.g. `27 passed, 244 unchanged (testmon)`. The unchanged tests passed previously and their code hasn't changed — they are verified. Do NOT use `--force` because the passed count looks low. `--force` is only needed for flaky test investigation or after non-code changes (config, env vars).

`dev test` accepts scope names (`database`, `pipeline`, `llm`), not file paths. Use `dev info` to see available scopes.

### What is blocked (and why)

| Blocked command | Use instead | Reason |
|---|---|---|
| `pytest ...` | `dev test` | No output management, wrong flags, no scoping |
| `ruff check/format` | `dev lint` / `dev format` | No output management |
| `basedpyright`/`pyright`/`mypy` | `dev typecheck` | No output management |
| `pytest ... \| grep/head/tail` | `dev test` | Buffering hangs, loses exit codes |
| `dev ... \| grep/head/tail` | Run `dev` directly | Output already captured to .tmp/dev-runs/ |
| `uv sync` | `uv pip install --system` | Creates .venv in devcontainer |
| `uv run ...` | Run commands directly | All tools are on PATH |

### What is allowed

`dev *`, `make *`, `uv pip install`, `uvx *`, `pytest --version`, `pytest --help`, `ruff --version`, `interrogate`, `vulture`, `semgrep`, `grep`, `pip install`.

---

## 9. Bash Guidelines

### Avoid commands that cause output buffering issues
- DO NOT pipe output through `head`, `tail`, `less`, `more`, or `grep` when monitoring or checking command output
- DO NOT use `| head -n X` or `| tail -n X` to truncate output - these cause buffering problems
- Instead, let commands complete fully, or use `--max-lines` flags if the command supports them
- For log monitoring, prefer reading files directly rather than piping through filter

### When checking command output:
- Run commands directly without pipes when possible
- If you need to limit output, use command-specific flags (e.g., `git log -n 10` instead of `git log | head -10`)
- Avoid chained pipes that can cause output to buffer indefinitely
