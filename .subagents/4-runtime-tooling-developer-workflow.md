# Runtime Behavior, Tooling, and Developer Workflow

This document covers the operational side of `ai-pipeline-core`: how code is run, tested, debugged, inspected, documented, and released. It is about the support layer around application code, not the shape of tasks, flows, or documents themselves.

## 1. Standard Development Workflow

Use the project’s `dev` CLI for all routine validation. Do not call `pytest`, `ruff`, or `basedpyright` directly for normal work. The framework expects one consistent execution path so output handling, test scoping, logging, and CI parity stay aligned.

### Required workflow
1. Run `dev info` first.
2. Make changes.
3. Run `dev format`.
4. Run `dev check --fast`.
5. Run `dev test`.
6. If needed, run `dev test --lf`.
7. Before committing, run `dev check`.

### Key commands
```bash
dev info
dev status

dev format
dev lint
dev typecheck

dev test
dev test --lf
dev test --full
dev test --available
dev test --coverage
dev test pipeline
dev test llm
dev test database
dev test deployment

dev check
dev check --fast
```

### Why this matters
- `dev test` uses testmon and project-specific scoping. A low “tests run” count is often correct, not a problem.
- `dev check` executes the project’s expected validation order: lint, types, dead code, semgrep, docstrings, tests.
- Output capture is already handled by the toolchain. Piping `dev ...` output through `grep`, `head`, or `tail` is explicitly discouraged because it breaks buffering, exit code handling, and log capture.

### Practical rules
- Prefer `dev test <scope>` over file-path-based `pytest` commands.
- Prefer `dev test --lf` after failures.
- Use `dev check --fast` while iterating and `dev check` before shipping.
- Assume long-running checks may take up to 10 minutes.

## 2. Persistence and Execution Stores

The framework supports three persistence modes, each for a different phase of work.

### ClickHouse
Use ClickHouse for production and realistic end-to-end runs. It is the canonical store for spans, documents, logs, and large-scale execution history.

Best when:
- Running real deployments
- Investigating production-like behavior
- Downloading or inspecting historical runs
- Aggregating cost and trace data over many runs

### Filesystem database
Use the filesystem backend for local debugging, replay, artifact inspection, and “single run as a folder” workflows.

Best when:
- You want one self-contained debug directory
- You need to inspect spans, documents, blobs, and logs offline
- You want to archive a run or hand it to another developer
- You want to replay or compare runs without infrastructure dependencies

Typical contents include:
- `spans/`
- `documents/`
- `blobs/`
- execution logs
- generated summaries like `summary.md`, `costs.md`, `documents.md`, `errors.md`

### In-memory database
Use the in-memory backend for fast unit tests and isolated logic checks.

Best when:
- Verifying pure behavior quickly
- You do not need persisted artifacts after the test
- You want zero setup and minimal latency

### Operational principle
Persistence is automatic. Application authors should not manually manage database writes. The value exposed to the user is full traceability and inspectability, not explicit storage APIs.

## 3. Inspection, Replay, and Debug Tooling

The framework supports two complementary debugging styles:
- inspect an existing run
- execute a focused debug run and inspect its artifacts

### `ai-trace`
`ai-trace` is the primary inspection tool for stored runs.

Typical usage:
```bash
ai-trace download <run-id> --output-dir .tmp/runs/<name>
ai-trace show <run-id> --db-path ./debug/run
ai-trace llm <run-id> --db-path ./debug/run
ai-trace docs <run-id> --db-path ./debug/run
ai-trace doc <sha256> --db-path ./debug/run
```

Use it when:
- You need a run tree
- You want to inspect LLM calls
- You want to inspect all documents from a run
- You want to view one concrete document body by SHA

### Replay workflow
The replay tooling exists for reconstructing or re-running execution from stored data.

Use replay when:
- You need to reproduce runtime behavior from captured inputs
- You want to compare behavior across framework versions
- You want to debug a run offline without hitting live systems

A practical workflow is:
1. Download a run from ClickHouse or collect a filesystem snapshot
2. Inspect it with `ai-trace`
3. Replay targeted spans or experiments in a filesystem-backed debug workspace

### Filesystem-backed debug execution
The framework supports local debug runs for tasks, flows, and conversations backed by a filesystem database. This is the fastest way to make a focused part of the pipeline observable without running the full system.

Key capabilities described in the development history:
- Run an individual task and persist its outputs, spans, logs, and blobs
- Run an individual flow with full trace capture
- Run a conversation in a debug context
- Generate artifacts even when execution fails
- Inspect the result with `ai-trace` afterward

A typical debug pattern is:
1. Create a debug output directory
2. Execute one task/flow/conversation against that directory
3. Inspect the captured artifacts with `ai-trace`

### Why filesystem debug runs matter
They let you:
- inspect the exact documents a unit of work emitted
- read execution logs without relying on centralized infrastructure
- understand LLM behavior with all prompt/response artifacts intact
- debug failures after the fact, not just live

## 4. Testing Patterns

The framework’s testing model has three layers: unit, integration, and regression.

### 4.1 Unit tests
Use unit tests for:
- pure document/model behavior
- type validation rules
- utility functions
- deterministic task logic that does not require a live model or external service

Preferred characteristics:
- in-memory database or no database
- no network
- no introspection-based “tests that verify the fix exists”
- assertions on real runtime behavior, not source code shape

### 4.2 Integration tests with real LLMs
Use integration tests when:
- you need to prove actual structured output behavior
- model/provider quirks matter
- replay/restore/caching/degeneration paths need end-to-end validation

Guidance from the work:
- use real models, no mocking
- use cheap but real models for most coverage
- mark integration tests explicitly
- guard on environment/API-key availability
- run provider tests in parallel where the external wait can be long

Typical integration concerns that were explicitly tested:
- structured list outputs
- nested structured models
- duplicate items not being silently deduplicated
- empty list outputs
- Unicode content in fields
- repeated structured calls in one conversation
- replay round-trip correctness
- restore/unwrap ordering for structured list outputs
- degeneration detector not tripping on legitimate pretty-printed structured output

### 4.3 Regression tests
Use regression tests to lock down discovered bugs.

The preferred workflow is:
1. write a failing or `xfail(strict=True)` test that proves the bug exists
2. fix the bug
3. remove `xfail` and keep the test permanently

This was repeatedly used to prove:
- invalid task/document return behavior
- provider edge cases
- null/empty response handling
- structured output decoding problems
- replay failures
- persistence and routing bugs

### 4.4 Provider tests
Providers have two kinds of tests:

#### Isolated provider tests
Use provider overrides or controlled transports to test:
- retry behavior
- auth failures
- timeout handling
- response parsing
- cost tracking
- override scoping

#### Real provider integration tests
Use real services and `.env` configuration to test:
- fire-and-forget behavior
- idempotent re-submit behavior
- polling semantics
- attachment recovery
- multi-provider parallel runs

### 4.5 Debugging-oriented test helpers
The evolution of the framework emphasized that tests should be easy to run in a debug mode with artifacts enabled. The useful pattern is:
- run with cheap real models by default for integration scenarios
- switch to filesystem-backed debug mode when a test fails and inspect the artifacts
- keep the runtime behavior identical; only the persistence destination changes

## 5. Provider Runtime Behavior

The provider layer is designed as infrastructure, not business state.

### Base model
Provider implementations are built on:
- `ExternalProvider`
- `StatelessPollingProvider`
- `ProviderOutcome`
- `ProviderError`
- `ProviderAuthError`

### Runtime semantics
#### Lazy initialization
Providers lazily create and own their HTTP clients. Application code should not need `async with` around provider usage. The provider encapsulates:
- client lifecycle
- locking around lazy init
- retries
- auth handling
- request helpers
- polling behavior

#### Infrastructure singleton pattern
Module-level provider singletons are valid when:
- they are assigned once
- they do not leak business state
- callers do not depend on prior calls by other tasks
- tests can override them safely
- the internal state is purely transport/infrastructure state

This is the explicit exception to the “no mutable global state” rule: infrastructure singletons are allowed when caller-stateless.

#### Override model for tests
Providers expose an override mechanism backed by `ContextVar`. This gives:
- per-test isolation
- concurrent test safety
- no process-global transport mutation requirement
- the ability to replace provider behavior for a single scope

### Stateless polling behavior
The framework and later application design converged on this runtime model:

- `wait=0` means fire-and-forget: submit work and return immediately
- a later call with the same semantic request and `wait>0` is allowed to re-submit
- the underlying service is expected to behave idempotently and continue polling the same logical job

This pattern matters because it lets one task start external work and another task collect the result later without carrying opaque handles through application code.

### Timeouts
Timeouts should represent the provider’s behavior, not force silent truncation. If a provider exceeds a wait budget:
- return a timeout result or raise a provider timeout signal
- do not slice result sets to fit declared limits
- do not silently drop work

Warnings are acceptable for “larger than expected” fan-out. Silent data loss is not.

### Attachment handling
Do not reinvent document or attachment serialization logic in provider integrations. The framework’s document and attachment classes already know how to:
- serialize bytes
- represent binary payloads as data URIs when needed
- deserialize stored content back into proper attachments

When reconstructing provider-produced documents, rely on framework deserialization (`from_dict` style reconstruction) instead of custom base64/data-URI parsing.

### Cost tracking
Providers can attach cost information to execution state. Cost attribution should stay inside the provider/runtime layer, not be manually replicated in application code.

## 6. LLM Runtime Behavior and Caching

### Structured output
The framework uses schema-driven structured output through `response_format`. Runtime expectations that emerged clearly:
- structured responses must be validated as real runtime behavior, not by source-inspection tests
- replay and restore paths must preserve structured outputs correctly
- list-like structured outputs require especially strong end-to-end testing

### Degeneration detection
A concrete historical failure mode was structured output being pretty-printed with large whitespace blocks, which accidentally triggered degeneration detection. The operational lesson is:
- degeneration detection must distinguish legitimate structured formatting from real degenerate output
- this needs regression and integration coverage against real models, not just unit assertions

### Prompt caching
The framework assumes:
- large shared context is acceptable
- identical prefixes are good
- input trimming and batching are usually the wrong optimization

Operationally:
- use consistent shared prefixes when appropriate
- do not try to micro-optimize prompt size by hand
- rely on provider-side caching behavior
- framework retries may deliberately disable cache when investigating bad model behavior

### Warmup/fork
The old warmup+fork pattern was explicitly removed from top-level guidance. It may still exist as a low-level behavior or historical test concept, but it is not the recommended authoring model for application developers.

## 7. Documentation Workflow and API Visibility

### `ai-docs`
The framework auto-generates developer-facing documentation from source.

Primary commands:
```bash
ai-docs generate
ai-docs check
make docs-ai-build
make docs-ai-check
```

Use them when:
- changing public API
- changing docstrings that are meant to become guides
- preparing a release
- investigating why `.ai-docs/` is stale in CI

### Public vs private API controls
Documentation visibility is controlled structurally:
- `_`-prefixed files and symbols are private
- public names are documented
- moving a module from public to private can remove low-value guide content
- making subclass hooks public can improve the usefulness of generated guides

This was used concretely to:
- improve the provider guide by exposing subclass-facing methods
- remove the logger guide by making logger internals fully private
- keep the generated README within its size limit

### Generated README constraints
The generated `.ai-docs/README.md` has a strict size limit. Operational consequences:
- public API bloat can break docs generation
- some utility functions may need to be privatized if they are not useful to application authors
- the docs generator itself may need compact rendering strategies to stay within size limits

### Pre-commit docs behavior
The workflow evolved toward auto-updating docs in the commit path rather than only failing on stale docs. The important operational point for developers is:
- changing public API often requires regenerated `.ai-docs/`
- version bumps can require regen because guide headers and references change
- do not treat doc generation as optional after API work

## 8. Release and Versioning Workflow

A typical release workflow is:

1. update version in `pyproject.toml`
2. run formatting and full checks
3. regenerate `.ai-docs`
4. verify README/guide size constraints
5. `git add`
6. commit
7. add tag
8. push commit and tags
9. if the GitHub release tag already exists, bump the patch version and repeat

### Common release pitfalls
- version bump without `.ai-docs` regen can make docs stale because guides embed versioned content
- adding public API can push the generated README over the size limit
- pre-commit may pass freshness while a full docs build still exposes a size issue if the docs were not regenerated locally
- if a release tag already exists remotely, create a new version instead of trying to reuse it

## 9. Tooling Compatibility and Known Operational Pitfalls

### Python syntax vs tooling versions
Modern Python syntax can outpace static tools. One concrete issue was semgrep missing findings in files using newer type-parameter syntax until the semgrep environment was upgraded.

Operational rule:
- when a rule “should have caught this” but did not, verify tool version support before assuming the rule is wrong

### Import-order and environment loading
If a project creates settings singletons at import time, environment loading must happen before heavy imports. Practical lesson:
- package `__init__` files should stay lightweight
- CLI entrypoints should load environment before importing modules that instantiate settings
- integration tests should replicate real import order, not only idealized direct imports

### Provider service quirks
Integration layers should document service behavior explicitly, especially when the correct usage is counterintuitive. A concrete example is the “submit early, re-submit later to poll the same logical request” pattern. This should be documented at the provider implementation level so developers do not “fix” correct behavior away.

## 10. Practical Debugging Playbook

When behavior is unclear:

### If it is a task or flow bug
- run it in a filesystem-backed debug context
- inspect spans and documents with `ai-trace`
- compare expected vs actual returned documents
- inspect whether the wrong artifacts were handed downstream or merely persisted

### If it is an LLM bug
- inspect exact prompt/response in trace output
- confirm structured output parsing, replay, and restore behavior
- reproduce with a real integration test before changing heuristics

### If it is a provider bug
- check whether the provider contract is idempotent
- inspect raw serialized document payloads before writing custom decoders
- verify timeout and auth behavior with real integration tests
- use provider override or scoped test replacement for isolated failure cases

### If it is a docs bug
- regenerate `.ai-docs`
- check public/private symbol visibility
- inspect whether the guide is missing subclass hooks because the API is still private
- check README size before and after API changes

## 11. What Application Developers Should Internalize

You should be able to rely on the framework for:
- persistence
- tracing
- LLM conversation capture
- provider retry/polling behavior
- debug artifact generation
- test isolation hooks
- API documentation generation
- release-time validation

You should not need to manually design around:
- database writes
- transport retry loops
- attachment serialization details
- prompt cache internals
- logging setup internals
- ad hoc trace persistence

The framework’s operational layer exists so application code can stay focused on business logic while still being fully debuggable and auditable.