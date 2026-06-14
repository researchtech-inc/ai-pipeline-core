# Content models define prompt-visible and renderer-hidden state

Content models are the structured boundary between application code, prompt context, documents, and framework-owned
completion. This file names the schema primitives that keep visible content, prompt-hidden fields, auto-filled fields,
and cited prose distinct. It does not define document factories, prompt execution, task wiring, pipeline planning, or
service integrations.

## Content schemas import field and wrapper names

Application schemas use one frozen model base, one field helper, and wrapper types that mark prompt visibility,
framework completion, and evidence support across documents, prompt outputs, tools, and run configuration.

### Reference

```python
from enum import StrEnum
from typing import Literal

from ai_pipeline_core import (
    AutoFilled,
    CitedText,
    DocumentCitation,
    ExcludedFromPrompt,
    Field,
    FrozenBaseModel,
)
```

`ExcludedFromPrompt[T]` stores a durable field while omitting it from prompt context. `AutoFilled[T]` declares a derived
field that the framework fills after model-authored or caller-supplied fields are validated. `CitedText` carries prose
with citations to other pipeline documents.

### Constraints

- Use `FrozenBaseModel` for structured boundary values that cross documents, prompt contracts, tools, tasks, phases,
  pipelines, or integrations.

## FrozenBaseModel defines boundary schemas

Boundary schemas are values that other framework surfaces rely on by type. When a content model appears in a document,
prompt output, tool payload, service payload, or run configuration, it becomes part of the public authoring contract.

### Constraints

- A boundary schema subclasses `FrozenBaseModel`.
- Field names are stable public names once another document, prompt contract, task, phase, pipeline, or integration
  relies on them.
- Nested structured values are named `FrozenBaseModel` subclasses when they carry meaning a later reader or caller
  relies on.
- Repeated boundary values use homogeneous tuple fields.
- A `FrozenBaseModel` becomes document content only when it represents durable business state.

### Examples

```python
from ai_pipeline_core import Field, FrozenBaseModel


class EvidenceSource(FrozenBaseModel):
    """One evidence source supplied to analysis."""

    source_name: str = Field(description="Human-readable source name.")
    body: str = Field(description="Full source text.")
```

## Field descriptions are required

Field descriptions are part of the visible contract because prompt context, generated reference, and fresh-agent edits
carry them forward. A field without a concrete description invites agents to fill the field's meaning from local
guesswork.

### Reference

`Field(description="Short field purpose.")` is required for public boundary fields. Allowed `Field` keyword arguments
on public boundary fields are:

- `description`, required unless the field is a `ClassVar`.
- `default`, when the field has a public default value.
- `default_factory`, when each instance needs a fresh public default value.
- `generator`, only on an `AutoFilled[T]` field.

### Constraints

- Every model-facing or boundary-schema instance field has a concrete `description`.
- The required-description rule covers document content fields, prompt contract input fields, prompt output fields,
  tool input fields, tool result payload fields, service payload fields, run configuration fields, and bundle fields.
- Task fields, phase fields, and service-client injected dependency fields use bare annotations as the visible default;
  `Field(description=...)` is optional for those code-facing fields.
- Pipeline subclasses do not declare instance fields.
- `ClassVar` metadata fields do not use `Field`.
- A description states what the field carries, not how the framework stores it.
- Other Pydantic `Field` keyword arguments, Pydantic field validators, aliases, patterns, length constraints, and
  arbitrary metadata are outside this public authoring surface.
- `PromptContract.validate` is the supported contract-level validation surface.
- The framework rejects unsupported field metadata at import time.

## Routed structures are concrete

Routing fields determine phase conditions, branch keys, validation outcomes, degradation states, and delivery choices.
A routed field must have a closed or otherwise precise shape because agents and type checkers need to know every
allowed value before the pipeline runs.

### Reference

Routed status fields use `StrEnum` or `Literal`. Boolean routing fields carry true-or-false semantics only. Repeated
routed values use homogeneous tuple fields.

### Constraints

- Values that code routes, gates, validates, filters, or branches on use typed fields, not prose.
- Routed status fields use `StrEnum` or `Literal`.
- Branch keys use closed values.
- Boundary schemas do not use `Any`, bare `object`, open `dict`, `Mapping`, or containers whose element type is
  untyped.
- Free-text status fields and loosely shaped payloads do not carry routing state.
- Domain identifiers may appear as content fields when the business specification names them.
- Domain identifiers are not document identity, provenance identity, or citation identity.
- Provenance and citation boundaries pass framework document objects or framework citation metadata, not domain
  identifier strings.

### Examples

```python
from enum import StrEnum
from typing import Literal

from ai_pipeline_core import Field, FrozenBaseModel


class ReviewStatus(StrEnum):
    ACCEPTED = "accepted"
    NEEDS_REPAIR = "needs_repair"
    BLOCKED = "blocked"


class ReviewDecision(FrozenBaseModel):
    """Decision used to route review work."""

    rationale: str = Field(description="Reason for the review route.")
    status: ReviewStatus = Field(description="Closed status controlling the next review step.")
    review_mode: Literal["standard", "expedited"] = Field(
        description="Whether the review follows the standard evidence path or the expedited path.",
    )
    confidence_score: float = Field(description="Confidence score from 0.0 to 1.0 for this decision.")
```

## ExcludedFromPrompt only changes prompt rendering

Some durable fields are for routing, validation, or review workflow and should not be shown to the model as evidence.
`ExcludedFromPrompt[T]` marks a normal durable content field that the prompt renderer skips. The field stays on the same
content model and remains accessible through `.content`.

### Constraints

- Use `ExcludedFromPrompt[T]` only for fields that must be durable and excluded from prompt context.
- A prompt-hidden field still has a concrete type and a concrete description.
- Application code reads a prompt-hidden field through `.content` like any other content field.
- Planning selectors address prompt-hidden fields through typed lambdas over document refs or document classes.
- Selector lambdas access prompt-hidden fields through `.content`.
- Fields hidden from prompts do not replace model-visible rationale when a human or model needs the reason for a
  decision.
- A document with business-significant prompt-hidden routing fields includes visible explanatory content for the
  routing choice.

### Examples

```python
from ai_pipeline_core import CitedText, ExcludedFromPrompt, Field, FrozenBaseModel


class ReviewResult(FrozenBaseModel):
    """Review result with model-visible rationale and prompt-hidden routing state."""

    rationale: CitedText = Field(description="Evidence-supported reason for the review result.")
    requires_repair: ExcludedFromPrompt[bool] = Field(
        description="True when the review result must be routed to repair.",
    )
```

## AutoFilled marks framework-completed fields

An auto-filled field is part of the public schema but not authored by the model or assigned by task code.
`AutoFilled[T]` marks an application-declared derived field whose generator the framework executes after ordinary
fields are validated. The generator is declared explicitly so the schema does not depend on field-name magic.

### Reference

`AutoFilled[T]` appears with a synchronous Python callable passed to `generator=` and a concrete `description=` value.
The callable receives the parent model instance after non-auto-filled fields are validated and returns the field type.

### Constraints

- `AutoFilled[T]` is allowed in document content models, prompt output models, `RunConfig`, and tool result payloads.
- `AutoFilled[T]` is forbidden in task fields, phase fields, pipeline fields, prompt contract input fields, tool input
  fields, bound tool fields, service client fields, and methodology class metadata.
- Every `AutoFilled[T]` field declares `generator=`.
- The generator callable returns the annotated field type.
- The generator callable is a pure, synchronous, deterministic function of the validated parent value.
- Generators run in field-declaration order.
- A generator may read fields declared earlier in the same model.
- A generator must not read another `AutoFilled` field, including one declared earlier.
- The generator callable does not perform I/O, read clocks, use randomness, call services, inspect global state, or
  mutate anything.
- Application code does not assign auto-filled fields directly.
- Field names alone do not trigger framework completion.
- The framework rejects auto-filled dependency cycles at import time.

### Examples

```python
from ai_pipeline_core import AutoFilled, Field, FrozenBaseModel


def count_supporting_documents(digest: "EvidenceDigest") -> int:
    return len(digest.supporting_document_titles)


class EvidenceDigest(FrozenBaseModel):
    """Digest with a framework-computed support count."""

    body: str = Field(description="Digest body prepared from the supplied evidence.")
    supporting_document_titles: tuple[str, ...] = Field(
        description="Evidence document titles supporting the digest.",
    )
    supporting_document_count: AutoFilled[int] = Field(
        generator=count_supporting_documents,
        description="Number of evidence documents the framework attached to this digest.",
    )
```

## CitedText marks claims that need document support

Generated prose that makes evidence claims needs attached support from other pipeline documents. `CitedText` marks that
prose at the leaf where document support is needed. Citable prose is typed differently from ordinary text so support
travels with the claim.

### Reference

`CitedText` exposes:

- `text: str`, the model-authored prose.
- `citations: tuple[DocumentCitation, ...]`, citations to other documents in the pipeline.

`DocumentCitation` is the framework-owned citation type. It exposes:

- `document_id: str | None`, the identity of the cited durable document; set for a document-backed citation.
- `url: str | None`, the cited URL; set only for an engine URL citation surfaced at the `PromptResult` level, never
  for `CitedText` support.
- `title: str | None`, an optional human-readable label for the cited source.
- `start_index: int | None`, the start of the supporting span.
- `end_index: int | None`, the end of the supporting span.
- `field: str | None`, the dotted path into a response model where the citation was collected; `None` for an engine
  URL citation.

`CitedText` support is document-backed: its `DocumentCitation` values set `document_id`. Provider URLs, web
citations, and external-source references are not `CitedText` citations. `DocumentCitation` is defined once here;
`api/4-prompt-contracts.md § PromptResult carries response and citations` describes the additional engine URL
citations that can appear at the `PromptResult` level. Application code cites a durable document by its framework
`id`; application-authored task inputs still use concrete document classes.

### Constraints

- Use `CitedText` for generated prose that makes evidence-grounded claims.
- Application code reads the prose through `cited_text.text`.
- Application code reads attached support through `cited_text.citations`.
- `CitedText` support comes from documents, not provider URLs, provider citations, or external-source strings.
- External evidence is captured as a document before generated prose can cite it.
- Do not store citations in a separate parallel field beside the prose they support.
- A citable claim keeps its supporting citations attached when the value is copied into a document.

### Examples

```python
from ai_pipeline_core import CitedText, Field, FrozenBaseModel


class ReviewConclusion(FrozenBaseModel):
    """Conclusion generated from review evidence."""

    conclusion: CitedText = Field(description="Evidence-supported review conclusion.")
    summary: str = Field(description="One-line display summary for human reviewers.")
```

## Reserved field names carry rendering roles

Several field names have established rendering roles because they appear frequently in document context. Use these
names only when the field plays that role.

### Reference

- `title` is rendered as the local heading for the item that owns it.
- `body` is rendered as the primary prose body for the item that owns it.
- `preamble` is rendered before the item's detailed fields.
- `citations` may only appear as document-citation metadata inside `CitedText`.

### Constraints

- Use a reserved field name only when the field plays that rendering role.
- Application schemas do not declare a field named `citations` at any other position.

### Examples

```python
from ai_pipeline_core import Field, FrozenBaseModel


class ReviewSection(FrozenBaseModel):
    """One rendered review-report section."""

    title: str = Field(description="Section heading.")
    preamble: str = Field(description="One-paragraph framing for the section.")
    body: str = Field(description="Primary section prose.")
```

## Rendered context is not application state

Prompt contracts receive a stable text view of prompt-visible document content. That view is for model context, not
for application code to parse or reproduce.

### Constraints

- Application code reads typed content fields, not rendered prompt context, to recover business state.
- Application code does not parse rendered prompt text for routing or persistence.

### Invariants

- `ExcludedFromPrompt` fields are omitted from prompt context.
- `ExcludedFromPrompt` fields remain durable fields on `.content`.
- `CitedText` fields carry document citations with the prose they support.
- Attachments remain associated with the document whose context is rendered.
