# Prompt contracts define model-mediated judgments

This file defines the public authoring contract for typed model interactions. It names how a `PromptContract`
declares purpose, inputs, returned value, success criteria, methodology, tool availability, paired body file,
template-driven rendering, result handling, validation, abstention, declared multi-step continuation, and terminal
failure. It does not define document rendering, model tool implementation, task persistence, model provisioning, the
renderer the framework selects, or the internal strategy the framework uses to satisfy one prompt contract.

## Prompt contracts import model-interaction names

A `PromptContract` declares one model-mediated judgment. The framework renders the user-prompt text from a paired
markdown body file and the contract's class metadata, input fields, output model, methodology attachments, and tool
availability. `PromptResult` carries the typed response and citations returned by execution.

### Reference

```python
from typing import ClassVar

from ai_pipeline_core import (
    AIModelRef,
    CitedText,
    Continues,
    DocumentCitation,
    Field,
    FrozenBaseModel,
    Methodology,
    PromptContract,
    PromptResult,
    TerminalError,
    ToolAvailability,
    ToolBinding,
    ValidationFailure,
)
```

The public execution shape is:

```text
async def execute(
    self,
    model: AIModelRef,
    *,
    tool_bindings: tuple[ToolBinding, ...] = (),
) -> PromptResult[OutputT]
```

### Constraints

- `PromptContract` subclasses use the `*Contract` suffix.
- `purpose`, `returns`, and `success_criteria` are required `ClassVar[str]` fields on every prompt contract, declared
  in the subclass's own `__dict__` (inherited values do not satisfy the requirement).
- Prompt-shaping metadata uses `ClassVar`; the framework rejects prompt-shaping metadata fields that omit it.
- Per-call inputs are instance fields with `Field(description=...)`.
- The generic parameter `OutputT` is bound to `FrozenBaseModel` (not plain `BaseModel`).
- Prompt execution returns `PromptResult[OutputT]` or raises `TerminalError`.
- Model identity is always `AIModelRef`. Wrap raw model names at the configuration boundary
  (`AIModelRef(name="model-id")`) and pass the handle through `RunConfig`, tasks, and contracts. `AIModelRef` is an
  opaque handle; application code passes it to `.execute(model=...)` and does not inspect, parse, or branch on it.
- `ToolBinding` values are created by calling `Tool.bind(**kwargs)` on the tool class; application code does not
  construct `ToolBinding` instances directly.
- `.execute(...)` takes no per-call generation options: temperature, system framing, reasoning effort, stop
  behavior, and similar generation settings are properties of the `AIModelRef` supplied at the configuration
  boundary or of the paired body file and attached methodology, not parameters of `.execute(...)`.

## PromptContract defines one model-mediated judgment

A prompt contract is the public boundary for one logical judgment delegated to a model. The framework renders the
model request from the contract declaration and its paired body file, then uses the typed output model to parse the
response. The caller relies on one typed result or one typed terminal failure.

### Reference

```python
from typing import ClassVar

from ai_pipeline_core import (
    CitedText,
    Field,
    FrozenBaseModel,
    Methodology,
    PromptContract,
    ToolAvailability,
    ValidationFailure,
)

from review_app.documents import ReviewEvidenceDocument
from review_app.tools import SearchEvidenceTool


class ReviewQualityMethodology(Methodology):
    """Guide review analysis across evidence-based prompt contracts."""

    purpose: ClassVar[str] = "Guide analysis toward evidence quality and support limits."


class ReviewAssessment(FrozenBaseModel):
    """Review assessment produced from supplied evidence."""

    body: CitedText = Field(description="Evidence-grounded assessment body.")
    unsupported: bool = Field(description="True when supplied evidence does not support a conclusion.")
    reviewed_evidence_ids: tuple[str, ...] = Field(
        description="Evidence identifiers the assessment relied on.",
    )


class AssessReviewRiskContract(PromptContract[ReviewAssessment]):
    """Assess one review-risk focus using supplied evidence."""

    purpose: ClassVar[str] = "Assess review risk for one focus using supplied evidence."
    returns: ClassVar[str] = "A review-risk assessment with cited body text and support status."
    success_criteria: ClassVar[str] = "The assessment cites supplied evidence and marks unsupported conclusions."
    methodologies: ClassVar[tuple[type[Methodology], ...]] = (ReviewQualityMethodology,)
    tools: ClassVar[tuple[ToolAvailability, ...]] = (ToolAvailability(SearchEvidenceTool, max_calls=4),)

    evidence: tuple[ReviewEvidenceDocument, ...] = Field(
        description="Evidence documents to assess, in source order.",
    )
    focus: str = Field(description="Review-risk question to assess.")

    def validate(self, response: ReviewAssessment) -> tuple[ValidationFailure, ...]:
        if response.unsupported and not response.body.text:
            return (ValidationFailure(message="Explain why supplied evidence does not support a conclusion."),)
        return ()
```

The contract's operational instructions live in a markdown file paired with the class (see _Paired body file_
below). The contract's `purpose`, `returns`, and `success_criteria` ClassVars supply the contract framing; the body
file supplies the analytical instructions the model reads.

### Constraints

- Prompt contracts capture model-mediated semantic judgment.
- Deterministic transformations that code can perform stay in task code.
- Bounded model-callable capability stays in model tools.
- Python-owned external work stays in service clients.
- One prompt contract does not hide unrelated judgments behind one output model.
- Multiple model-mediated judgments are represented as multiple prompt contracts.

## Class metadata drives generated prompts

The contract docstring gives the Python class its ordinary code-facing summary. `purpose`, `returns`, and
`success_criteria` are prompt-facing declarations the framework renders alongside the body file when it sends the
prompt to the model.

### Constraints

- `purpose` is required on every prompt contract.
- `purpose` names the model-mediated judgment, not the downstream task or phase.
- `returns` is required on every prompt contract.
- `returns` names the response value represented by the output model, not the document later built from that response.
- `success_criteria` is required on every prompt contract.
- `success_criteria` states the declarative response conditions the model must satisfy.
- The three required declarations use exactly `purpose`, `returns`, and `success_criteria`.
- Prompt contracts do not introduce additional prompt-summary class variables.
- Field descriptions stay on fields.
- Response-level success stays in `success_criteria`.
- Downstream use is expressed by task, document, and phase wiring, not prompt metadata.

## Paired body file supplies the operational instructions

Long-form analytical instructions live in a single `.j2` file paired with the contract class. The framework
discovers the file at class definition time by stripping the `Contract` suffix, snake-casing the remainder, and
looking next to the Python module that defines the class:

- `<stem>.j2` — a Jinja2 template, rendered against the documented template context. A template that contains no
  Jinja tags is effectively static markdown; there is no separate static extension.

A paired body file is mandatory for non-exempt application contracts: a contract subclass that ships no `<stem>.j2`
file raises `TypeError` at class definition time. Framework-internal classes, test modules, and example code are
exempt from the file requirement and render only the contract framing
(purpose / returns / success criteria / methodology / input fields).

### Constraints

- The body file name is derived from the class name (suffix stripped, PascalCase → snake_case) and lives next to
  the defining Python module.
- A paired body file is rejected when it contains an H1 header (`# `) outside fenced code blocks; H1 is reserved
  for framework section boundaries in the rendered prompt.
- A `.j2` template must parse under Jinja2; syntax errors surface at class definition time.
- A `.j2` template references only the documented template-context names (below). An undefined name is an error
  surfaced at render time, not a silently blank substitution.
- The `to_json` filter is available in `.j2` templates and renders any value as stable indented JSON.
- The body file must be non-empty.
- A paired body file is required for every non-exempt contract subclass; the framework raises `TypeError` at class
  definition time when no `<stem>.j2` file exists next to the defining Python module.
- A framework-exempt class (tests, framework internals) skips the file requirement and renders only the contract
  framing with no `# Instructions` section.

## Template context names what a `.j2` file may reference

A `.j2` template is rendered against a fixed, documented context. These are the only names a template author may
reference; they are stable and the framework supplies them from the contract declaration and its inputs. The author
writes a template against this schema and never assembles prompt text or selects a renderer directly.

The top-level template names are:

- `contract` — the contract's framing: `purpose`, `returns`, `success_criteria`, `title`, `docstring`.
- `inputs` — one entry per non-document instance field, keyed by field name; each carries `title`, `kind`
  (`"scalar" | "structured" | "enum" | "none"`), `type_name`, and a pre-rendered `text`.
- `input_order` — the declaration order of the input fields.
- `documents` — references to the supplied document items. Document content itself is supplied to the model as
  cacheable context, not interpolated into the user-prompt text.
- `methodologies` — one entry per attached methodology, each with `title`, `purpose`, `body` (the rendered
  methodology body file), and any public string ClassVars declared on the methodology.
- `output` — the structured output model's identity.
- `tools` — one entry per declared tool, each with `name`, `description`, and `max_calls`.
- `citations` — citation framing for the contract.
- `notation` — URL-preservation guidance, present only when URL substitution applied to this prompt (see _URL
  substitution_ below).

A tag-free `.j2` body renders to itself and is emitted under a framework-owned `# Instructions` section; it need
not reference any of these names.

### Constraints

- A `.j2` template references only the names above and the `to_json` filter; ad-hoc context extensions are not
  supported.
- Application code does not assemble prompt text, build the render context, or select a renderer; the framework
  renders from the contract declaration and its paired body file.

## Input fields declare model context

Prompt-contract inputs become the model context for one judgment. Their field descriptions tell the model how each
value participates in this interaction, not what the Python type means in general.

### Reference

Supported input field shapes are:

- Required document: one `Document` subclass.
- Optional document: one `Document` subclass with `| None`.
- Document collection: `tuple[DocumentSubclass, ...]`.
- Scalar value: a concrete primitive such as `str`, `int`, `float`, or `bool`.
- Closed set: `StrEnum` when shared or `Literal["initial", "follow_up"]` when local to one contract.
- Structured value: a `FrozenBaseModel` subclass.

### Constraints

- Every input field uses `Field(description="Field role in this interaction.")`.
- Input fields name the exact context for one judgment.
- A long source text, imported artifact, previous model output, or durable business value is passed as a document
  input.
- A previous task-local prompt response may be passed as a structured input to a follow-up contract inside the same
  task when the follow-up needs the prior answer as data; declared conversation continuation uses `continues=`
  instead (see _Multi-step continuation is declared, not improvised_).
- A document tuple preserves the order supplied by the caller.
- Scalar fields carry small parameters that scope the judgment, not durable evidence bodies.
- Broad active state, all available documents, and nearby unrelated documents are not prompt inputs.
- Document-typed inputs are partitioned automatically: they are placed into the conversation as cacheable context
  messages, not interpolated into the user-prompt text.

## Output models define response shape

The output model is the public response contract. The framework requests that shape, parses into that shape,
validates that shape, and returns that shape through `PromptResult.response`.

### Constraints

- The output type is a `FrozenBaseModel` subclass (the `PromptContract[OutputT]` generic is bound to
  `FrozenBaseModel`).
- Every output field has `Field(description="Field role in this response.")`.
- Output fields come from the business specification or downstream code requirements.
- Output fields are not added to compensate for prompt or model-output uncertainty.
- Narrative evidence-grounded prose uses `CitedText`.
- When an output model declares both explanatory fields and routing or decision fields, explanatory fields appear
  first in the model class.
- A model-authored routing or decision field is paired with visible explanatory output in the same response model.
- Optional fields use `T | None` with an explicit default when absence is part of the contract.

## Abstention is a valid response, not a failure

A model-mediated judgment may have no supportable answer: the supplied evidence is insufficient, contradictory, or
out of scope. An accepted response that says so is a success, not an error. The output model represents that state
explicitly so an abstention is typed data the caller can route on, never a fabricated conclusion or a silent
default.

### Reference

```python
from enum import StrEnum

from ai_pipeline_core import CitedText, Field, FrozenBaseModel


class SupportStatus(StrEnum):
    """Whether supplied evidence supports a conclusion."""

    SUPPORTED = "supported"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"


class RiskFinding(FrozenBaseModel):
    """Risk finding that may abstain when evidence is insufficient."""

    rationale: CitedText = Field(description="Evidence-grounded reason for the finding or for abstaining.")
    support: SupportStatus = Field(description="Whether supplied evidence supports a conclusion.")
```

### Constraints

- An output model represents "no supportable conclusion" with a typed field or closed status value, not by omitting
  the answer or returning an empty string.
- A well-formed abstaining response is an accepted `PromptResult`, not a `ValidationFailure`.
- `validate` does not reject an abstention that satisfies the response shape; it does not push the model toward a
  positive conclusion the evidence does not support.
- The repair loop never coerces an abstaining response into a conclusion.
- An abstaining response still carries its explanatory `CitedText`; abstention is explained, not blank.
- Task code carries abstention forward as durable state through a typed outcome document (see
  `6-tasks.md § Insufficient evidence is carried forward, not overwritten`).

### Anti-patterns

Wrong: the output model has only a `conclusion: str` field, so a model with insufficient evidence invents a weak
conclusion to fill it. Correction: add a closed `support` status (or an optional conclusion with an explicit
default) so "insufficient evidence" is a first-class response value.

Wrong: `validate` returns a `ValidationFailure` whenever the response abstains, forcing the repair loop to
manufacture a conclusion. Correction: accept the abstention; validate the *shape* of the response, not the
presence of a positive answer.

## Methodology declares class-level analytical guidance

A methodology is reusable analytical instruction attached to prompt contracts that share one way of reasoning. It
is a class-only declaration, never instantiated by application code.

Every non-exempt `Methodology` subclass MUST have a paired `.j2` body file discovered the
same way as a contract body file: strip the `Methodology` suffix, snake-case the remainder, look next to the
defining Python module. The body file IS the operational analytical content the model reads. The class declaration
carries only the framework-required hint label. As with prompt contracts, framework-internal classes, test modules,
and example code are exempt from the body-file requirement and render only the methodology's hint label.

### Reference

```python
from typing import ClassVar

from ai_pipeline_core import CitedText, Field, FrozenBaseModel, Methodology, PromptContract


class MethodologyAssessment(FrozenBaseModel):
    """Assessment output used by the methodology attachment example."""

    body: CitedText = Field(description="Evidence-grounded assessment body.")


class ReviewQualityMethodology(Methodology):
    """Guide review analysis across evidence-based prompt contracts."""

    purpose: ClassVar[str] = "Guide analysis toward evidence quality and support limits."


class AssessReviewRiskContract(PromptContract[MethodologyAssessment]):
    """Assess one review-risk focus using supplied evidence."""

    methodologies: ClassVar[tuple[type[Methodology], ...]] = (ReviewQualityMethodology,)
```

The paired body file `review_quality.j2` supplies the methodology's rubric language,
scoring criteria, and worked instructions. The framework loads this file at class definition time and surfaces the
rendered result as the methodology's `body` under the `methodologies` template name (see _Template context names
what a `.j2` file may reference_).

### Constraints

- A prompt contract attaches methodology classes through `methodologies: ClassVar[tuple[type[Methodology], ...]]`.
- A methodology declares `purpose: ClassVar[str]` — a short hint label. This is the only framework-required
  ClassVar on a methodology.
- A methodology does not declare instance fields, does not use `Field`, and is never instantiated by application
  code (attempting to instantiate one raises `TypeError`).
- A methodology does not declare `returns` or `success_criteria`.
- Methodology subclass names end with `Methodology`.
- Per-contract methodology changes are expressed by subclassing the methodology class and pairing it with its own
  body file.
- A methodology's operational body lives in a paired `.j2` file; missing files are rejected at class definition
  time.
- Other public string ClassVars on a methodology are surfaced to templates alongside that methodology's `body`
  under the `methodologies` template name, and appear beneath the methodology body in a non-templated render.
  Prefer putting analytical content in the body file rather than spreading it across additional ClassVars.
- Case-specific evidence remains an input field on the contract, not methodology content.
- Body files must not contain H1 headers (`# `) outside fenced code blocks; H1 is reserved for framework section
  boundaries.

## Tool availability makes bounded capabilities visible

Tool availability is a prompt-contract declaration because the model decides whether to call a bounded capability
during the interaction. The prompt contract names the available tool classes and the maximum calls for each class;
execution supplies bound context separately.

### Reference

```python
from typing import ClassVar

from ai_pipeline_core import ToolAvailability, ToolBinding
from review_app.tools import SearchEvidenceTool


tools: ClassVar[tuple[ToolAvailability, ...]] = (ToolAvailability(SearchEvidenceTool, max_calls=4),)

# Application code constructs bindings at execute() time via Tool.bind(**kwargs):
binding: ToolBinding = SearchEvidenceTool.bind(corpus=evidence_corpus)
```

Import `ToolBinding` from `ai_pipeline_core`, like every other boundary name.

### Constraints

- A prompt contract declares available tools through `tools: ClassVar[tuple[ToolAvailability, ...]]`.
- Every `ToolAvailability(tool=..., max_calls=...)` declares a positive integer `max_calls`.
- Bound tool values are supplied through `tool_bindings=` at `.execute()` time, constructed via
  `Tool.bind(**kwargs)`.
- `max_calls` is enforced per-tool across all rounds in one prompt execution, including any rounds added by the
  validation repair loop.
- A tool class declared in `tools` must have a matching binding in `tool_bindings`; a binding for an undeclared
  tool is rejected with `TypeError`.
- Tool availability does not prove coverage; the output model records reviewed items and validation compares them
  with the required set.

## Validation returns one failure shape

Validation turns parsed model output into accepted response content or concrete repair messages. An empty tuple
means accepted, and one or more `ValidationFailure` values describe what must be changed.

### Reference

Contract-level validation has this shape:

```text
def validate(self, response: OutputT) -> tuple[ValidationFailure, ...]
```

`ValidationFailure` is a frozen model constructed with keyword arguments:

- `message: str`, the concrete repair instruction.
- `field: str | None = None`, the response field the repair targets when one field can be named.

### Constraints

- `validate` is synchronous.
- Contract-level validation reads only `self` and `response`.
- Validation does not perform I/O, read clocks, use randomness, call services, inspect global state, or mutate
  anything.
- Validation failure messages name the concrete response repair.
- Application code does not catch validation failure and fabricate a success document.
- The repair budget is framework-owned and not tunable per call; its configuration key is named in
  `5-configuration.md § Environment configuration`.

## PromptResult carries response and citations

`PromptResult[OutputT]` is the public result wrapper returned by prompt execution. It carries the typed response
and the citations the framework collected while satisfying `CitedText` fields and any URL citations attached by
search-enabled models. A prompt result becomes a durable document only when task code builds a returned document
from that response.

### Reference

`PromptResult[OutputT]` exposes exactly two fields:

- `response: OutputT`
- `citations: tuple[DocumentCitation, ...]`

`DocumentCitation` is the framework-owned citation type, defined once in
`api/3-content-models.md § CitedText marks claims that need document support`. At the `PromptResult` level its
values are of two kinds:

- **Document-backed citations**, harvested from every `CitedText` field reachable inside the parsed response, with
  `document_id` set and `field` stamped to the dotted path where the citation was found (e.g. `"body"`,
  `"findings[0].summary"`).
- **Engine URL citations**, surfaced from search-enabled models, with `url` set and `document_id` and `field` left
  `None`.

Engine URL citations are result-level metadata, not `CitedText` support; `CitedText` (defined in `api/3`) carries
its `text` and its document-backed `citations` only.

### Constraints

- `PromptResult.citations` is the combined execution-level set: engine URL citations (with `field=None`) merged
  with the citations harvested from every `CitedText` field reachable inside the parsed response (with `field`
  stamped to the dotted path the walker visited).
- Cited prose remains modeled with `CitedText`.
- Citations do not replace typed response fields.
- `PromptResult` does not expose cost, duration, span identifiers, model identity, or per-round transport
  metadata; that data is recorded in the execution span, not the prompt result.

## URL substitution honors the AIModelRef capability flag

The framework replaces long URLs in supplied context with short placeholders so the model sees a compact prompt,
then restores them on the parsed response. Substitution runs only when the active `AIModelRef` declares
`supports_url_substitution=True` (the default). Set `supports_url_substitution=False` on an `AIModelRef` whose
provider returns text grounded to literal URLs (e.g. search-enabled deployments) to disable substitution end to
end for executions that use that model.

When substitution applies, the `notation` template name is populated so a `.j2` template can include the URL
preservation instruction in the user-prompt text.

## Multi-step continuation is declared, not improvised

Several prompt calls may participate in one coherent task-local judgment. There are two distinct task-local
patterns, and they answer different needs:

- **Pass a prior typed response as data.** A follow-up contract receives an earlier contract's typed response as an
  ordinary structured input field. The follow-up runs as a fresh model interaction that happens to read that value.
  Use this when the next judgment needs the prior *answer* but not the prior *reasoning*.
- **Continue the same conversation.** A continuation contract declares that it continues a named opener contract, so
  the model retains the prior turn's conversation state across the step. Use this when the next judgment must build
  on the model's own prior reasoning, not just its final answer.

Continuation is declared on the class through `continues=`, using `Continues.once(...)` or
`Continues.repeating(...)`. The declared opener is the only contract this one may continue, which makes the lineage
checkable at import time rather than discovered at runtime.

### Reference

```python
from typing import ClassVar

from ai_pipeline_core import Continues, Field, FrozenBaseModel, PromptContract, PromptResult


class DraftOpinion(FrozenBaseModel):
    """First-pass opinion produced by the opener contract."""

    body: str = Field(description="Draft opinion text.")


class DraftOpinionContract(PromptContract[DraftOpinion]):
    """Produce a first-pass opinion from the brief."""

    purpose: ClassVar[str] = "Produce a first-pass opinion from the brief."
    returns: ClassVar[str] = "A draft opinion."
    success_criteria: ClassVar[str] = "The opinion addresses the brief."

    brief: str = Field(description="Brief the opinion responds to.")


class ReviseOpinionContract(
    PromptContract[DraftOpinion],
    continues=Continues.once(DraftOpinionContract),
):
    """Revise the drafted opinion in the same conversation after reviewer notes."""

    purpose: ClassVar[str] = "Revise the prior opinion using reviewer notes."
    returns: ClassVar[str] = "A revised opinion."
    success_criteria: ClassVar[str] = "The revision addresses every reviewer note."

    prior: PromptResult[DraftOpinion] = Field(description="The draft turn this revision continues.")
    notes: str = Field(description="Reviewer notes to apply.")
```

`Continues.once(Opener)` declares a single continuation of `Opener`. `Continues.repeating(Opener)` declares a step
that may continue `Opener` and then continue itself across further turns — the shape used by bounded refinement
loops and multi-round debate. The predecessor execution is supplied as a `PromptResult`-typed field; the framework
reads both the prior typed response and the conversation thread from that field. There is no `continue_from=`
parameter on `.execute(...)`.

### Constraints

- A continuation contract declares its lineage with `continues=Continues.once(Opener)` or
  `continues=Continues.repeating(Opener)`, where `Opener` is a `PromptContract` subclass.
- A continuation contract's legal predecessor set is exactly `{Opener}` for `Continues.once` and `{Opener, Self}`
  for `Continues.repeating`.
- The predecessor execution enters through one `PromptResult`-typed input field. `PromptResult` is an allowed input
  field type only on a contract that declares `continues=`.
- The `PromptResult` field's type parameter is the output model of a contract in the legal predecessor set; the
  framework rejects a predecessor outside that set at import time.
- A contract that declares no `continues=` shares no conversation thread with any other contract; it is a fresh
  single judgment anchored only on its declared inputs.
- `.execute(...)` receives no conversation-history parameter, and application code does not reconstruct prompt
  conversation history manually.
- Continuation stays task-local: a continuation lineage does not cross a task or phase boundary. Durable state
  crosses boundaries as documents, not as conversation objects.
- The worked call sites — a `Continues.once` follow-up and a `Continues.repeating` bounded loop — live in
  `6-tasks.md § Continuation contracts run inside one task`.

### Isolation is provable at import time

Because lineage is declared on the class and not wired at runtime, the framework checks anchoring before any run
starts. A verifier contract that judges a candidate and declares no `continues=` lineage to the contract that
produced the candidate cannot inherit that contract's conversation or reasoning state — it sees only the typed
inputs it declares. Independence between a producing judgment and a contract that reviews it is therefore a
property the framework proves from the declarations, not something a reader has to confirm by inspecting prompt
bodies.

### Anti-patterns

Wrong: a verifier contract continues the generator it is supposed to check, so the verifier inherits the
generator's framing and rubber-stamps it. Correction: the verifier declares no `continues=` lineage and receives
the candidate as an ordinary typed input, so its judgment is anchored independently.

Wrong: a task threads a conversation by hand, passing accumulated message history into `.execute(...)`. Correction:
declare `continues=` on the follow-up contract and let the framework own the thread.

## Prompt execution raises TerminalError when no valid response is produced

When prompt execution cannot produce an accepted response, it raises `TerminalError`. A terminal prompt failure is
the task boundary's failure, not an invitation to fabricate a success document.

### Constraints

- Application code does not catch terminal prompt failure and fabricate a success document.
- The terminal message names the contract and the response issue that prevented success.
