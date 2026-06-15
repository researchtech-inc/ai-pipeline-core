# Writing Tests

This file is the reference for authoring an application's tests. A test is ordinary asynchronous Python that
exercises one unit through the runner and asserts on the recorded result. The enforceable contract for the runner
methods a test calls is owned by `advanced-api/1-runners-and-clients.md`; this file is how an author composes them
into a test.

## A test runs a unit through the runner

A test constructs a unit — a task instance, a prompt contract instance, or the pipeline — and runs it through a
`Runner`, then asserts on the returned `RunResult` or `ContractRunResult`. The runner records the unit exactly as a
full run does, so the test's record is inspectable and replayable when it fails.

- A task test calls `runner.run_task(task_instance, inputs=...)` and asserts on the `RunResult`.
- A prompt-contract test calls `runner.run_contract(contract_instance, model=...)` and asserts on the
  `ContractRunResult`.
- A whole-pipeline or phase-range test calls `runner.run(pipeline, inputs=..., run_config=...)` or
  `runner.run_range(...)` and asserts on the delivered output bundle.

A `Runner()` constructed with no configuration is filesystem-backed and in-process, so a test needs no external
infrastructure (`advanced-api/1-runners-and-clients.md § Local execution needs no infrastructure`).

### Examples

A task test — exercise one task with resolved document inputs and assert it completed:

```python
from ai_pipeline_core import AIModelRef, RunStatus, Runner

from review_app.documents import ReviewEvidenceDocument, ReviewRequestDocument
from review_app.tasks.assess_risk import AssessRiskTask


async def test_assess_risk_completes(model_ref: AIModelRef) -> None:
    runner = Runner()
    request = ReviewRequestDocument.create_root(content=..., name="request")
    evidence = (ReviewEvidenceDocument.create_root(content=...),)
    result = await runner.run_task(
        AssessRiskTask(model=model_ref),
        inputs={"request": request, "evidence": evidence},
    )
    assert result.status is RunStatus.COMPLETED
    assessment = result.outputs
```

A prompt-contract test — exercise one contract and assert on its typed response:

```python
from ai_pipeline_core import AIModelRef, RunStatus, Runner

from review_app.documents import ReviewEvidenceDocument
from review_app.prompt_contracts import AssessEvidenceContract


async def test_assess_evidence_marks_unsupported(model_ref: AIModelRef) -> None:
    runner = Runner()
    evidence = ReviewEvidenceDocument.create_root(content=...)
    outcome = await runner.run_contract(AssessEvidenceContract(evidence=evidence), model=model_ref)
    assert outcome.status is RunStatus.COMPLETED
    assert outcome.result.response.unsupported is True
```

A test driving a unit constructs its root inputs the same way an in-process driver does — through `create_root`
(`advanced-api/1-runners-and-clients.md § Driver code constructs root documents at the run boundary`) — because a
test is in-process driver code.

## Constraints

- A test exercises a unit through the runner (`run`, `run_range`, `run_task`, `run_contract`); it does not call a
  task's `run` method or a contract's `execute` method directly.
- A test asserts on the recorded `RunResult`/`ContractRunResult` (`status`, `outputs`/`result`, `error`), not on
  side effects observed outside the record.
- A test lives in the package's `tests/` directory and imports the runner surface from `ai_pipeline_core` and the
  application's units from the package.
- A test does not reach into framework internals to run a unit without recording, and does not construct a faster
  unrecorded path.
- A test reads recorded spans, documents, provenance, and cost through the read seam
  (`advanced-api/2-database.md`), the same surface production diagnosis uses.

## Authoring constraints the quality pipeline enforces

The suite is run through `dev test` (`testing/1-overview.md § The development quality surface`), which places every
test in a lane and enforces a fixed set of structural constraints. These are not procedures for using the tool; they
are constraints on what a valid test *is*. A test that violates one is rejected by the automated quality pipeline
before it reaches review.

- A test completes within the timeout of the lane it lives in: **30 seconds** in `tests/unit/`, **180 seconds** in
  `tests/integration/`, **900 seconds** in `tests/qualification/`. A test that exceeds its lane timeout is a
  wrong-shape test, not a slow one — it moves to the lane its cost belongs in.
- Unit tests (`tests/unit/`) do not make live model calls; they patch the provider boundary or use the local
  in-process runner. Live model interaction belongs in the integration or qualification lane.
- A test parameterizes on one axis per function. Stacked `@pytest.mark.parametrize` decorators build a multiplicative
  matrix; the correct shapes are one `parametrize` per function, separate functions for separate axes, or the
  qualification lane when a matrix is genuinely needed.
- A test does not pass token-cap keyword arguments (`max_completion_tokens`, `max_output_tokens`, `max_tokens`):
  truncation causes flakes, so it uses framework defaults.
- A test does not call `asyncio.sleep` with a duration of one second or more outside `tests/qualification/`; long
  waits belong in the qualification lane.
- A test does not use `Path(".")` (it is xdist-unsafe); it uses `tmp_path` or an explicit project fixture.
- A test does not define custom pytest runner hooks (`pytest_pyfunc_call`, `pytest_sessionfinish`); it relies on the
  framework's pytest plugin and the lane policy.
- A suppression annotation (`# noqa`, `# type: ignore`, `# nosemgrep`, `@pytest.mark.xfail`) carries a one-line
  invariant justification on the same line.

## Anti-patterns

Wrong: a test calls `AssessRiskTask(...).run(request, evidence)` directly to avoid the runner, so the execution
leaves no record and cannot be inspected or replayed when it fails. Correction: run the task through
`runner.run_task`, which records it exactly as a full run does (`advanced-api/1-runners-and-clients.md`).

Wrong: a test asserts an exact model output string. Correction: a model judgment is re-sampled, so a behavior test
asserts on the typed, routable fields of the response (a status, a closed value, a structural property), not on
token-for-token output (`4-limits-and-non-promises.md § One-shot correctness of model output`).
