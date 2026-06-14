# Model tools expose bounded model-callable capabilities

This file defines the public authoring contract for tools a prompt contract can expose to the model during one prompt
execution. It names model-visible arguments, bound execution context, prompt-execution-local state, availability,
scoped document lookup, document-returning visibility, and typed failure. It does not define service-client
integrations, prompt validation rules, task retry policy, observability records, or durable-store browsing.

## Model tools import execution-boundary names

Model tools are Python classes made visible by a prompt contract for one execution. The prompt contract chooses which
tool classes are available, and the model chooses whether to invoke an available tool within the declared bound.

### Reference

```python
from ai_pipeline_core import (
    BoundField,
    Document,
    Field,
    FrozenBaseModel,
    GetDocumentByIdTool,
    RetryableError,
    StatelessTool,
    StatefulTool,
    StatefulToolResult,
    TerminalError,
    ToolAvailability,
    ToolBinding,
)
```

## Tools are bounded model-callable capabilities

A model tool exists when the model needs a narrow capability during one prompt execution and the model is the actor
deciding whether that capability is useful. Python-owned service work belongs behind a service client; model-decided
inspection, lookup, search, or transformation belongs behind a model tool.

### Constraints

- A tool is available only inside the prompt execution whose prompt contract declared it.
- Model tools do not perform task routing, phase routing, pipeline routing, or retry policy.
- Application-authored tools do not browse the durable store.
- The durable store and its database API are part of the advanced programmatic surface (`advanced-api/`); they are
  never reachable from a model tool.
- `GetDocumentByIdTool` is the only framework-provided tool that resolves documents by identity.

## Model-visible and bound fields stay separate

Tool fields have two authoring shapes. A model-visible field uses `Field` with a concrete description because the
model supplies that value. A bound field uses `BoundField` with a concrete description because Python supplies that
value before the prompt execution and the model does not see it as an argument.

### Reference

Allowed `BoundField` keyword arguments are:

- `description`, required.
- `default`, optional when the bound field has a public default value.

Calling a tool class's `bind` method with its bound field values returns a `ToolBinding` accepted by prompt execution.

### Constraints

- Model-supplied tool arguments use `Field(description=...)`.
- Python-supplied prompt context uses `BoundField(description=...)`.
- Repeated values use `tuple[T, ...]`.
- The tool class's `bind` method supplies bound field values to the prompt contract through `tool_bindings=`.
- Bound fields do not carry routing decisions.
- Unsupported `BoundField` keyword arguments are rejected at import time.

## ToolBinding supplies execution context

`ToolBinding` is an opaque token that connects a tool class to Python-supplied bound values for one prompt execution.
Prompt contracts receive bindings at execution so prompt declaration and task-local context stay separate.

### Reference

```text
ToolClass.bind(bound field values) -> ToolBinding
```

### Constraints

- A tool binding is created by the tool class whose bound fields it satisfies.
- Multiple bindings for different tool classes are passed as a tuple to `tool_bindings=`.
- A tool class is bound at most once per prompt execution.
- Application code does not inspect or construct `ToolBinding` values directly.

## StatelessTool returns a typed payload or document

A stateless tool has no memory across invocations. A prompt-local result returns a `FrozenBaseModel` payload. A result
that must become durable state returns a concrete `Document` subclass or a payload that contains a returned document
field.

### Reference

```text
class SomeTool(StatelessTool):
    async def execute(self) -> ResultT
```

### Constraints

- A stateless tool does not declare state.
- A stateless tool does not store prompt-execution data in class attributes, module variables, or mutable defaults.
- A prompt-local stateless tool returns a `FrozenBaseModel` payload.
- A prompt-local payload that exposes text copied from documents carries the source document `id` beside any domain
  identifier.
- A prompt contract that expects `CitedText` from document text surfaced by a tool makes the source documents
  resolvable through prompt inputs, bound context, returned documents, or `GetDocumentByIdTool`.
- A tool returns a concrete `Document` subclass when the document is the whole tool result.
- A tool returns a typed payload containing a document field when the model also needs a typed miss, status, or
  degradation value beside the document.
- A document returned by a tool records immediate provenance through a document factory.
- A document returned by a tool is durable in the run record and resolvable through `GetDocumentByIdTool` for the same
  prompt execution.
- `CitedText` may cite a document returned by a tool while the prompt execution is active.
- After the prompt execution ends, downstream phases see a tool-returned document only if the enclosing task returns it
  or includes it in a returned bundle.

### Examples

```python
from ai_pipeline_core import BoundField, Field, FrozenBaseModel, StatelessTool

from review_app.documents import ToolEvidenceDocument, ToolEvidenceNote, ToolEvidenceNoteDocument


class EvidenceLookupMatch(FrozenBaseModel):
    """One evidence item matched by lookup."""

    document_id: str = Field(description="Framework document id for the matched evidence document.")
    evidence_id: str = Field(description="Stable evidence identifier.")
    title: str = Field(description="Evidence title.")
    body: str = Field(description="Evidence body relevant to the query.")


class EvidenceLookupResult(FrozenBaseModel):
    """Evidence lookup result returned to the model."""

    matches: tuple[EvidenceLookupMatch, ...] = Field(
        description="Evidence items whose bodies match the query, with source document ids.",
    )


class LookupEvidenceTool(StatelessTool):
    """Return matching evidence items from prompt-bound evidence."""

    query: str = Field(description="Plain-language query to match against evidence.")
    evidence: tuple[ToolEvidenceDocument, ...] = BoundField(
        description="Evidence documents bound by application code for this prompt execution.",
    )

    async def execute(self) -> EvidenceLookupResult:
        query = self.query.casefold()
        matches = tuple(
            EvidenceLookupMatch(
                document_id=document.id,
                evidence_id=document.content.evidence_id,
                title=document.content.title,
                body=document.content.body,
            )
            for document in self.evidence
            if query in document.content.body.casefold()
        )
        return EvidenceLookupResult(matches=matches)


class CaptureEvidenceNoteResult(FrozenBaseModel):
    """Result of attempting to capture a prompt-bound evidence note."""

    note: ToolEvidenceNoteDocument | None = Field(
        default=None,
        description="Captured note document when the evidence identifier was available.",
    )
    missing_evidence_id: str | None = Field(
        default=None,
        description="Evidence identifier that was not available in the bound prompt context.",
    )


class CaptureEvidenceNoteTool(StatelessTool):
    """Capture one prompt-bound evidence document as a durable note."""

    evidence_id: str = Field(description="Evidence identifier to capture.")
    evidence: tuple[ToolEvidenceDocument, ...] = BoundField(
        description="Evidence documents bound by application code for this prompt execution.",
    )

    async def execute(self) -> CaptureEvidenceNoteResult:
        selected = tuple(document for document in self.evidence if document.content.evidence_id == self.evidence_id)
        if not selected:
            return CaptureEvidenceNoteResult(missing_evidence_id=self.evidence_id)
        evidence_document = selected[0]
        note = ToolEvidenceNoteDocument.from_content_sources(
            content=ToolEvidenceNote(
                title=evidence_document.content.title,
                body=evidence_document.content.body,
            ),
            content_sources=(evidence_document,),
        )
        return CaptureEvidenceNoteResult(note=note)
```

## StatefulTool carries state inside one prompt execution

A stateful tool remembers information across invocations during one prompt execution. Cross-prompt memory is
represented by durable documents or service clients.

### Reference

`StatefulTool.State` is an inner `FrozenBaseModel` subclass. Every field on `State` declares a default or
`default_factory=`, and `execute` has this shape:

```text
async def execute(self, state: State) -> StatefulToolResult[ResultT]
```

### Constraints

- `StatefulTool.State` is a `FrozenBaseModel` subclass.
- Every `State` field declares a default or `default_factory=`.
- The framework constructs initial state by calling `Tool.State()` with no arguments.
- A stateful tool's `execute` method accepts `State` and returns `StatefulToolResult[ResultT]`.
- `StatefulToolResult` is parameterized by the model-visible result payload.
- The state type comes from the tool's inner `State` class.
- Cross-prompt memory is represented by durable documents or service clients.

### Examples

```python
from ai_pipeline_core import Field, FrozenBaseModel, StatefulTool, StatefulToolResult


class QueryHistoryResult(FrozenBaseModel):
    """Query-history result returned to the model."""

    normalized_query: str = Field(description="Normalized query text.")
    duplicate: bool = Field(description="True when this query already ran in this prompt execution.")


class QueryHistoryTool(StatefulTool):
    """Track duplicate queries inside one prompt execution."""

    query: str = Field(description="Search query to record.")

    class State(FrozenBaseModel):
        """State scoped to one prompt execution."""

        seen_queries: tuple[str, ...] = Field(
            default=(),
            description="Normalized queries already submitted in this prompt execution.",
        )

    async def execute(
        self,
        state: State,
    ) -> StatefulToolResult[QueryHistoryResult]:
        normalized_query = " ".join(self.query.casefold().split())
        duplicate = normalized_query in state.seen_queries
        next_seen_queries = state.seen_queries if duplicate else state.seen_queries + (normalized_query,)
        return StatefulToolResult(
            result=QueryHistoryResult(
                normalized_query=normalized_query,
                duplicate=duplicate,
            ),
            state=QueryHistoryTool.State(seen_queries=next_seen_queries),
        )
```

## ToolAvailability declares available tools

`ToolAvailability` names one tool class and the fixed call bound for that tool during one prompt execution. The prompt
contract class declares the available tool classes.

### Reference

```python
from typing import ClassVar

from ai_pipeline_core import ToolAvailability
from review_app.tools import LookupEvidenceTool


tools: ClassVar[tuple[ToolAvailability, ...]] = (ToolAvailability(LookupEvidenceTool, max_calls=4),)
```

### Constraints

- `max_calls` is a positive integer declared with the prompt contract class.
- `max_calls` is not computed from model output or runtime document content.

## GetDocumentByIdTool resolves scoped documents

`GetDocumentByIdTool` is the framework-provided lookup tool for resolving a document identifier already in scope for
one prompt execution. Its model-visible argument is `document_id: str`. The resolvable identifiers are the identifiers
of documents supplied through prompt inputs, bound context, or document-returning tools in the same prompt execution.

### Reference

```python
ToolAvailability(GetDocumentByIdTool, max_calls=6)
```

### Constraints

- `GetDocumentByIdTool` resolves only documents scoped to the current prompt execution.
- The `document_id` argument is the framework-owned `document.id`, not a domain identifier such as `evidence_id`.
- Application code scopes documents through prompt inputs and bound context, not store access.
- Out-of-scope identifiers are rejected as terminal prompt-execution failures.

## Tool failures are typed

Tool failures use the same failure vocabulary as task boundaries. A retryable integration failure raises
`RetryableError`; a terminal condition that requires an external fix raises `TerminalError`. An expected domain miss
that the model can reason from returns a typed payload instead of raising.

### Constraints

- Tool authors raise `RetryableError` for retry-eligible integration failures.
- Tool authors raise `TerminalError` for terminal failures that require an external fix.
- Expected domain misses return typed degradation payloads.
- A model-supplied value outside the bound prompt context returns a typed degradation payload when the result type
  explicitly represents that miss.
- A model-supplied value outside the bound prompt context is terminal when the result type does not represent that
  miss.
- Programming errors fail prompt execution; they are not converted into model evidence.

## Coverage belongs to output validation

A tool invocation proves only that one bounded capability ran with one set of arguments. It does not prove that every
required entity, source, claim, account, component, or evidence document was checked. Coverage is represented in the
prompt contract's output model and verified by validation.

### Constraints

- Tool availability and the fact that a tool ran are not coverage evidence.
- The output model records reviewed items, and validation compares them with the required set.

### Anti-patterns

Wrong: a prompt treats one search invocation as proof that required evidence was reviewed. Correction: the output model
declares reviewed item identifiers, and validation compares them with the required set.
