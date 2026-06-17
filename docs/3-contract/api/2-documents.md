# Documents carry durable pipeline state

This file defines the public document contract for `ai-pipeline-core` applications. Documents are durable artifacts
with identity, typed content, immediate provenance, revision history, optional names, attachments, citation metadata,
and bundle grouping. It does not define content-model field behavior, prompt execution, task wiring, phase planning,
pipeline delivery, or service integrations.

## Document classes import durable-state names

Document modules import only the framework boundary names needed to declare durable artifacts and groups. The public
surface is small because document classes carry state; they do not schedule work.

### Reference

```python
from ai_pipeline_core import Attachment, Document, DocumentBundle, DocumentCitation, Field, PromptResult
```

## Documents expose durable state through content

A document is the unit the framework persists and hands between pipeline steps. The content value is the business
payload; the document wrapper carries identity and durable metadata around that payload.

### Reference

Every document instance exposes these public fields and attributes:

- `id: str`, an opaque framework-assigned identifier.
- `name: str | None`, an optional human-readable metadata label.
- `attachments: tuple[Attachment, ...]`, byte material owned by the document.
- `content: T`, the typed content value for task code, factories, and selector lambdas.
- `revision`, the revision metadata view for immutable evolution.
- `content_sources`, immediate documents whose content contributed to this document.
- `causal_sources`, immediate documents that triggered creation of this document.

Application code reads business fields through `.content`, such as `document.content.rating`. Planning selectors use
typed lambdas over document refs or document classes and access durable fields through `.content`; no separate routing
namespace exists.

### Constraints

- Application code never reads structure from `id` or constructs identifiers.
- A document is immutable after creation.
- Application code does not mutate `document.content`, `document.attachments`, or nested document fields.
- New durable state is represented by a new document created through a document factory.
- All durable content fields are reached through `.content`.
- `ExcludedFromPrompt[T]` changes prompt rendering only; it does not move the field out of `.content`.
- Revision-aware work reads `number`, `previous`, and `chain` through `document.revision`.
- The document wrapper exposes `name` as its only human-readable label; it has no `description` or `summary` field.
  A summary or description that must travel with the artifact is a field on the content model.
- Orchestration/correlation labels are not document data: a document has no label field, and such labels are not
  document content, identity, or provenance. Correlation labels live on the run, and document factories do not accept
  them (`runtime-api/2-run-control.md § Correlation labels are immutable run metadata`).

## Content shapes choose the storage boundary

The type argument to `Document[T]` names what kind of payload crosses the durable boundary. The same wrapper is used for
structured analytical state and for boundary text or binary payloads; the type argument is the distinction.

### Reference

Legal document content shapes are:

- `Document[T]` where `T` is a `FrozenBaseModel` subclass, for structured pipeline state.
- `Document[str]` for imported text, exported text, rendered report text, or other text boundary payloads.
- `Document[bytes]` for imported binary payloads, exported binary payloads, or other binary boundary payloads.

### Constraints

- Documents represent business artifacts named as durable pipeline state.
- Analytical state uses a `FrozenBaseModel` subclass.
- `str` and `bytes` documents are boundary payloads, not a shortcut around structured analytical state.
- Use `Document[bytes]` when the binary payload is itself a durable artifact that a later phase reads as input or the
  pipeline delivers as output.
- Use `Attachment` for byte material that companions one structured document and never crosses a phase boundary on its
  own.
- A binary or text boundary that later feeds analysis is converted into structured documents by a task.
- Temporary lookup payloads, prompt scaffolding, adapter values, local accumulators, and convenience groupings stay as
  `FrozenBaseModel` values or Python locals, not documents.
- A typed job document that carries one bounded unit of fan-out work is a durable document, not scaffolding: it is
  the per-item input a later phase fans out over when a dependent or derived collection cannot be expressed as a
  simple homogeneous input collection (see `api/7-phases.md`).
- Composite content models group fields only when the grouped value is one business artifact.
- Unrelated outputs use separate document types or `DocumentBundle` fields.

### Examples

```python
from ai_pipeline_core import Document, Field, FrozenBaseModel, Task


class RegistryLookupRequest(FrozenBaseModel):
    """Request to capture one registry filing."""

    company_id: str = Field(description="Registry company identifier to inspect.")


class RegistryLookupRequestDocument(Document[RegistryLookupRequest]):
    """Registry lookup request supplied to a filing-capture task."""


class RegistryFiling(FrozenBaseModel):
    """Filing captured from an external registry."""

    title: str = Field(description="Filing title.")
    body: str = Field(description="Filing body.")


class RegistryFilingDocument(Document[RegistryFiling]):
    """Registry filing available to later pipeline phases."""


class CaptureRegistryFilingTask(Task):
    """Capture one registry filing as durable evidence."""

    async def run(self, request: RegistryLookupRequestDocument) -> RegistryFilingDocument:
        return RegistryFilingDocument.from_external_source(
            content=RegistryFiling(
                title="Director list changed",
                body="The current filing names a different director list.",
            ),
            source_id=f"registry-company-{request.content.company_id}-filing",
            causal_sources=(request,),
        )
```

## Factories record immediate provenance

Document factories are the only public creation path for non-root documents. The factory name states why the new
document exists and which immediate upstream documents caused or contributed to it.

### Reference

Factories are classmethods on concrete document subclasses and use keyword-only arguments.

The bare `Document` annotation in factory provenance parameters means any durable document from the run record. This
is a framework-owned provenance boundary, not permission to use bare `Document` in task inputs or application schemas.

```text
from_content_sources(
    *,
    content: T,
    content_sources: tuple[Document, ...],
    causal_sources: tuple[Document, ...] = (),
    name: str | None = None,
    attachments: tuple[Attachment, ...] = (),
) -> Self

from_causal_sources(
    *,
    content: T,
    causal_sources: tuple[Document, ...],
    name: str | None = None,
    attachments: tuple[Attachment, ...] = (),
) -> Self

from_external_source(
    *,
    content: T,
    source_id: str,
    causal_sources: tuple[Document, ...] = (),
    name: str | None = None,
    attachments: tuple[Attachment, ...] = (),
) -> Self

from_prior_revision(
    *,
    prior: Self,
    content: T,
    content_sources: tuple[Document, ...] = (),
    causal_sources: tuple[Document, ...] = (),
    name: str | None = None,
    attachments: tuple[Attachment, ...] = (),
) -> Self

from_prompt_result(
    *,
    result: PromptResult[T],
    causal_sources: tuple[Document, ...] = (),
    content_sources: tuple[Document, ...] = (),
    name: str | None = None,
    attachments: tuple[Attachment, ...] = (),
) -> Self
```

### Constraints

- Application code creates non-root documents only through document factories.
- Factory calls record immediate upstream documents only.
- Root documents enter through pipeline input bundles.
- Application tasks do not create root documents through document factories.
- Use `from_content_sources` when upstream documents contributed content.
- Use `from_causal_sources` when upstream documents triggered work without becoming transformed content.
- Use `from_external_source` when a document records an observation from outside the pipeline.
- Service results that observe external content use `from_external_source` with `source_id=` derived from the typed
  service request or submission.
- Service receipts that record the act of submission use `from_causal_sources`.
- Use `from_prior_revision` when the new document replaces an earlier version of the same durable artifact.
- The `prior=` document passed to `from_prior_revision` has the same concrete document class as the revised document.
- Pass `content_sources=` to `from_prior_revision` only when documents other than the prior revision contributed
  content to the new revision.
- The prior document is recorded through revision linkage, not duplicated in `content_sources`.
- Use `from_prompt_result` when a prompt contract result becomes durable state.
- Prompt input documents are automatically recorded as prompt-result content sources.
- Documents cited by `CitedText` values in the prompt response are automatically recorded as prompt-result content
  sources.
- Explicit `content_sources=` and `causal_sources=` on `from_prompt_result` are merged with auto-derived sources and
  deduplicated by document identity.
- Pass `causal_sources=` to `from_prompt_result` to record a request document or other durable state that caused the
  prompt to run without appearing as prompt content.
- `source_id` is a stable opaque label; the framework does not dereference it or treat it as document identity.
- Documents named in `content_sources` and `causal_sources` are real durable documents from the run record.
- Application code does not instantiate documents directly, call `model_copy`, copy documents with dump-and-validate
  pairs, cast between document types, or hide factory choice behind helper functions.
- Application code does not construct placeholder documents only to satisfy provenance arguments.

### Anti-patterns

Wrong: a task copies all upstream ancestors into a new document so later code can see the full chain directly.
Correction: record only immediate upstream documents and let the framework recover ancestry through links.

Wrong: a side-effect receipt is recorded as a content transformation of the request document. Correction: use causal
provenance because the request triggered the side effect.

## Root documents are created at the run boundary

Root documents are created by the code that drives a run, not by application-authored tasks, phases, pipelines,
prompt contracts, or tools. The authoring surface names the root document types and the input bundle they populate;
constructing those root documents from raw input — bytes, files, or a fetched source — belongs to the surface that
drives the run. An in-process driver builds the root input bundle through the advanced programmatic surface
(`advanced-api/1-runners-and-clients.md`); an out-of-process driver supplies each root input as raw external
material — inline content or a fetchable reference — that the runtime resolves into a typed root document at the run
boundary (`runtime-api/2-run-control.md § Root inputs enter as raw external material`). By-identity rehydration of a
stored document is an in-process capability only (`advanced-api/1-runners-and-clients.md § Stored documents enter a
run by identity`); the out-of-process boundary has no by-identity input. The two boundaries are intentionally
different: an in-process driver can import the document definitions and construct typed root documents, while an
out-of-process driver cannot, so it supplies raw external material that the runtime resolves. The out-of-process
boundary is not a remote typed-document construction surface and does not gain one.

### Constraints

- Application authoring code does not construct root documents; root documents enter only through the pipeline input
  bundle.
- A task that finds it needs a root document has a design error: the value enters as a root input, or is produced by
  a task through a non-root factory.
- Authoring code does not hide root-document creation behind a helper inside the application package.

## Revisions preserve prior state

Revision creates a new immutable document that supersedes a prior document without changing the prior document. The
prior version remains durable, citable, and recoverable.

### Reference

Revision metadata is read through `document.revision`:

- `number: int`, the monotonically increasing revision number for this artifact.
- `previous: Document | None`, the prior document when this document is a revision.
- `chain: tuple[Document, ...]`, ordered revision history for the same durable artifact.

### Constraints

- A revision creates a new document identity.
- A revision uses the same document class as the prior document.
- Revision metadata is read through `document.revision`.
- Work that changes the semantic artifact uses revision.
- Work that creates a different artifact uses another document type.

### Invariants

- The prior document remains unchanged.
- The revised document points to the prior document.
- Every document exposes `document.revision`.
- For root documents and first-created documents, `document.revision.number` is `1`.
- For root documents and first-created documents, `document.revision.previous` is `None`.
- For root documents and first-created documents, `document.revision.chain` is a one-element tuple containing that
  document.

## Provenance accessors expose immediate sources

Provenance accessors give application code the immediate source documents it is allowed to reason from directly.
Typed provenance slices return only sources of the requested document class.

### Reference

```text
def content_sources_of_type[D: Document](self, document_class: type[D]) -> tuple[D, ...]
def causal_sources_of_type[D: Document](self, document_class: type[D]) -> tuple[D, ...]
```

`DocumentCitation` values are attached to `CitedText`; provenance accessors do not replace citations on generated
prose.

### Constraints

- Business logic uses immediate provenance accessors.
- Business logic does not reconstruct the full provenance graph for routing.
- A source document appears in the content or causal collection according to why it was used.
- A prior revision is not also copied into `content_sources` only to make it easier to find.

## Document names are optional metadata

A document name is an optional human-readable label for the artifact, not the durable identity. Names make generated
bundles and exported artifacts easier to inspect, while identity remains framework-owned.

### Reference

`document.name` is `str | None`. Application code may pass `name=` to any document factory.

### Constraints

- Application code may pass `name=` when an artifact needs a stable public label.
- If `name=` is omitted, `document.name` is `None`.
- Application code does not use `name` as identity.
- Repeated documents of the same class may share a name when their identities differ.
- Planning and delivery select documents by type, not by `name`. Documents that a later phase or the delivered
  output must distinguish are separate document types or fields of a `DocumentBundle`; documents that differ only by
  `name` are not separately selectable.
- Names use stable semantic labels, not timestamps or generated identifiers.

## DocumentBundle names public document groups

A bundle is a named group of document fields that crosses a public boundary. Pipeline inputs, pipeline outputs, child
pipeline inputs, and multi-output task returns use bundles so field names become part of the contract rather than an
incidental tuple order.

### Reference

Bundle fields use ordinary annotations with `Field(description=...)`:

```python
from ai_pipeline_core import DocumentBundle, Field


class ReviewOutputs(DocumentBundle):
    """Documents delivered after one review run."""

    report: ReviewReportDocument = Field(description="Review report delivered by the run.")
    repair: RepairDocument | None = Field(
        default=None,
        description="Repair document when the run produced one.",
    )
    evidence: tuple[EvidenceDocument, ...] = Field(
        description="Evidence documents delivered by the run.",
    )
```

### Constraints

- A bundle subclass names a public group of documents.
- Bundle fields have stable names.
- Every bundle field declares `Field(description=...)`.
- Heterogeneous document groups use named bundle fields.
- Homogeneous repeated document groups use tuples of one concrete document class.
- Optional fields use `T | None` with an explicit default when absence is part of the contract.
- A bundle does not carry scalar configuration or service clients.
- A `DocumentBundle` subclass instance carries resolved documents at runtime.
- A `DocumentBundle` subclass instance carries planning refs only when constructed inside a phase builder for
  `f.child_pipeline(inputs=...)`.
- The framework resolves planning refs to documents before the child pipeline boundary executes.

### Anti-patterns

Wrong: a task returns a heterogeneous tuple because two documents are produced together. Correction: define a bundle
with named fields for the two documents.

Wrong: a pipeline output bundle includes scalar flags beside documents. Correction: put those flags inside a document
content model.

## Attachments belong to one document

Attachments are companion artifacts that travel with one document. They keep binary or auxiliary material attached to
the durable state that gives the material meaning.

### Reference

`Attachment` is a frozen model with this construction shape:

```text
Attachment(
    *,
    name: str,
    content: bytes,
    media_type: str | None = None,
    description: str | None = None,
)
```

```python
report_pdf = Attachment(
    name="report.pdf",
    content=rendered_report_bytes,
    media_type="application/pdf",
    description="Rendered review report.",
)
```

An attachment carries an optional human-readable `description`, unlike the document wrapper (whose only label is
`name`), because an attachment has no content model of its own to hold descriptive prose. The runtime read model
surfaces that description (`runtime-api/5-documents-and-logs.md`).

### Constraints

- Attachments are immutable after construction.
- A task that needs to route on attachment-derived facts writes those facts into document content.
- Attachment byte material does not cross phase boundaries on its own.

### Invariants

- Child pipeline and export behavior preserve attachments with the owning document.
