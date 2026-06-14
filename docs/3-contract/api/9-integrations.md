# Integrations expose typed external boundaries

This file defines the public contract for external execution boundaries in `ai-pipeline-core` applications. It names
when application code uses a model tool, service client, polling client, or child pipeline, and what typed shape each
boundary exposes. It does not define transport, deployment, credentials, operations configuration, or framework
internals below those boundaries.

## Integration modules import external-boundary names

Integration code names the actor that owns an external decision. The public surface keeps model-chosen tools,
Python-owned service calls, long-running service observations, and child pipeline invocation separate.

### Reference

```python
from ai_pipeline_core import (
    Field,
    FrozenBaseModel,
    PollFailed,
    PollPending,
    PollReady,
    PollResult,
    PollingServiceClient,
    RetryableError,
    ServiceClient,
    TerminalError,
)
```

`ServiceClient` is the Python-owned request boundary. `PollingServiceClient` is the submit-and-observe boundary.
`PollResult` is the discriminated observation family returned by a polling check.

## Integration surface follows decision owner

The same external system can be reached through different surfaces depending on who chooses the action. The authoring
contract keeps those choices separate so task code, prompt contracts, and pipeline composition do not hide one actor
behind another.

### Reference

- A model tool is used when the model chooses whether to call a bounded capability.
- `ServiceClient` is used when Python task code chooses one request-and-response service call.
- `PollingServiceClient` is used when service output may need later observation.
- `f.child_pipeline` is used when the target has its own bundle types, run configuration, and run record.

### Constraints

- Python-owned service decisions do not become model tools.
- Model capability is not wrapped in a service client.
- Child pipeline work does not hide behind a service method.
- Runner-supplied model capability is carried through `RunConfig`, not through an integration class.
- Application integrations do not read from or write to the framework's own durable store through a service client;
  specialized database access is part of the advanced programmatic surface (`advanced-api/`), not ordinary
  application authoring.

## ServiceClient wraps Python-owned service calls

A service client is used after task code has already decided that a service call belongs in the current task. The
service client field is populated before the task runs, and task code reads the client field from `self`.

### Reference

A registry service client has this public shape:

```text
class RegistryClient(ServiceClient):
    async def fetch_company_record(self, request: CompanyLookupRequest) -> CompanyRecord
```

### Examples

```python
from ai_pipeline_core import Field, FrozenBaseModel


class CompanyLookupRequest(FrozenBaseModel):
    """Registry lookup request."""

    company_id: str = Field(description="Registry company identifier to look up.")


class CompanyRecord(FrozenBaseModel):
    """Company record observed from a registry."""

    company_id: str = Field(description="Registry company identifier returned by the service.")
    legal_name: str = Field(description="Registered legal name returned by the service.")
```

```text
class RegistryClient(ServiceClient):
    registry_region: str

    async def fetch_company_record(self, request: CompanyLookupRequest) -> CompanyRecord
```

### Constraints

- A service client is a task field, not a runtime input.
- The service client field is populated before `run` starts.
- Service clients are frozen model-shaped Python classes.
- Service client fields use bare annotations as the visible default.
- `Field(description=...)` is optional for service client injected dependency and configuration fields.
- Configuration exposed to application authors is declared through `RunConfig` or service client fields.
- Application authors implement typed service methods.
- Transport helpers, credentials loading, deployment retry, backoff policy, and rate limits are not represented in task
  code.
- Service client methods are named for the application observation they return, such as `fetch_company_record`, not for
  transport actions.
- Service client code does not create task inputs, documents, retry loops, or credential loaders.

## Process-local transport state stays behind integration boundaries

A service client may hold mutable process-local transport infrastructure — for example an async HTTP session, SDK
client, connection pool, or auth-refresh cache. That infrastructure is recreated for each process that runs the
task and is never serialized or persisted. It is allowed because it is plumbing, not run state: it does not carry
business data, task progress, or hidden cross-task memory, and it does not change what a task means. What a task
means still depends only on its declared inputs, declared fields, external responses, and returned documents.

### Constraints

- A service client may keep mutable process-local transport infrastructure only for external communication.
- That infrastructure is recreated per process as needed; application code does not assume one client instance
  persists across tasks or runs.
- A service client does not store run state, business data, hidden progress, prior task results, or cross-task
  coordination state.
- Application code does not read or coordinate through process-local transport state directly.
- If information must outlive the call, it leaves the task as a document or receipt, not as client-held memory.

## ServiceClient methods return typed payloads

A service client method is a typed adapter between a service observation and application terms. Application authors
implement typed async methods and keep transport setup, credentials, retry policy, and rate limits below the public
task boundary.

### Constraints

- Request and response payloads are `FrozenBaseModel` subclasses.
- A request with one identifier is still wrapped in a named request payload model.
- A response containing one scalar value is still wrapped in a named response payload model.
- Service client methods are async methods.
- Service client methods raise `RetryableError` for retry-eligible failures.
- Service client methods raise `TerminalError` for terminal failures.
- Service client methods do not return untyped mappings, bare scalars, documents, or SDK payloads.

## PollingServiceClient separates submission from observation

A polling service accepts work before the final result exists. `PollingServiceClient` separates the submission that
creates a typed service identity from the observation that checks that identity once. Durable pipeline state carries
that identity when submission and collection happen in different tasks, phases, or runs.

### Reference

Polling clients are generic in request, submission, and result payloads:

```text
class PdfConversionClient(
    PollingServiceClient[PdfConversionRequest, PdfConversionSubmission, ConvertedPdf],
):
    async def submit(self, request: PdfConversionRequest) -> PdfConversionSubmission
    async def check_status(self, submission: PdfConversionSubmission) -> PollResult[ConvertedPdf]
```

### Examples

```python
from ai_pipeline_core import Field, FrozenBaseModel, PollReady, PollResult, PollingServiceClient


class PdfConversionRequest(FrozenBaseModel):
    """Request to convert one stored PDF."""

    source_key: str = Field(description="Service-visible key for the source PDF.")


class PdfConversionSubmission(FrozenBaseModel):
    """Typed identity for one submitted PDF conversion."""

    job_id: str = Field(description="Service job identifier for the submitted conversion.")
    region: str = Field(description="Service region that owns the submitted conversion.")


class ConvertedPdf(FrozenBaseModel):
    """Completed PDF conversion result."""

    text: str = Field(description="Text extracted from the converted PDF.")


class PdfConversionClient(
    PollingServiceClient[PdfConversionRequest, PdfConversionSubmission, ConvertedPdf],
):
    """Submit PDF conversion work and observe one submitted conversion."""

    region: str

    async def submit(self, request: PdfConversionRequest) -> PdfConversionSubmission:
        return PdfConversionSubmission(job_id=f"pdf-{request.source_key}", region=self.region)

    async def check_status(
        self,
        submission: PdfConversionSubmission,
    ) -> PollResult[ConvertedPdf]:
        return PollReady(value=ConvertedPdf(text=f"Converted text for {submission.job_id}."))
```

### Constraints

- `RequestT`, `SubmissionT`, and `ResultT` are `FrozenBaseModel` subclasses.
- The application defines the submission payload type for each polling client.
- One polling client class represents one request, submission, and result family.
- A service with two independent polling endpoints declares two polling client classes.
- `submit` and `check_status` are async methods.
- `submit` returns the application's submission payload type.
- `submit` raises `RetryableError` for retry-eligible submission failures.
- `submit` raises `TerminalError` for terminal submission failures.
- `check_status` performs one observation and returns `PollResult[ResultT]`.
- `check_status` raises `RetryableError` for retry-eligible observation failures.
- `check_status` raises `TerminalError` for terminal observation failures that prevent a valid polling observation.
- Service-level pending and failed states are reported through `PollResult`.
- Task code does not sleep, poll in loops, or keep service identities in process memory.

## PollResult carries one polling observation

`PollResult[T]` is one observation of one submitted service identity. It is a discriminated family so pending, ready,
and failed states cannot be represented with invalid value combinations.
Application code imports `PollResult` for return annotations and branches on the concrete observation classes.

### Reference

```text
PollResult[T] = PollPending | PollReady[T] | PollFailed
```

`PollPending` exposes:

- `status: Literal["pending"]`

`PollReady[T]` exposes:

- `status: Literal["ready"]`
- `value: T`

`PollFailed` exposes:

- `status: Literal["failed"]`
- `error: str`

### Constraints

- `PollPending` carries no result value.
- `PollReady[T]` carries the typed result value.
- `PollFailed` carries a service-level job failure message.
- Application code branches with `match` or `isinstance`.
- Application code does not construct polling observations by combining status, value, and error fields manually.

## Receipt documents preserve split submit and collection

When submission and collection happen at different boundaries, the service identity is pipeline state. The submit task
returns a receipt document, and the collect task receives that receipt before it checks the identity once.

### Examples

```python
from ai_pipeline_core import Document, Field, FrozenBaseModel, PollFailed, PollPending, PollReady
from ai_pipeline_core import RetryableError, Task, TerminalError

from review_app.integrations import ConvertedPdf, PdfConversionClient, PdfConversionRequest, PdfConversionSubmission


class SourcePdf(FrozenBaseModel):
    """Source PDF submitted for conversion."""

    source_key: str = Field(description="Service-visible key for the source PDF.")


class SourcePdfDocument(Document[SourcePdf]):
    """Source PDF document supplied to conversion tasks."""


class PdfConversionReceipt(FrozenBaseModel):
    """Receipt for one submitted PDF conversion."""

    submission: PdfConversionSubmission = Field(description="Typed service identity for status checks.")


class PdfConversionReceiptDocument(Document[PdfConversionReceipt]):
    """Receipt document returned after submitting PDF conversion."""


class ConvertedPdfDocument(Document[ConvertedPdf]):
    """Converted PDF document returned after service completion."""


class SubmitPdfConversionTask(Task):
    """Submit one PDF conversion and return its receipt."""

    client: PdfConversionClient

    async def run(self, source: SourcePdfDocument) -> PdfConversionReceiptDocument:
        submission = await self.client.submit(
            PdfConversionRequest(source_key=source.content.source_key),
        )
        return PdfConversionReceiptDocument.from_causal_sources(
            content=PdfConversionReceipt(submission=submission),
            causal_sources=(source,),
        )


class CollectPdfConversionTask(Task):
    """Check one submitted PDF conversion once."""

    client: PdfConversionClient

    async def run(self, receipt: PdfConversionReceiptDocument) -> ConvertedPdfDocument:
        observation = await self.client.check_status(receipt.content.submission)
        match observation:
            case PollPending():
                raise RetryableError("PDF conversion is still pending.")
            case PollFailed(error=message):
                raise TerminalError(f"PDF conversion failed: {message}")
            case PollReady(value=converted):
                return ConvertedPdfDocument.from_external_source(
                    content=converted,
                    source_id=receipt.content.submission.job_id,
                    causal_sources=(receipt,),
                )
```

### Constraints

- A submit task records the typed submission identity in document content.
- Submission and collection use a stable external identity carried in pipeline state, so a repeated submit or a
  repeated observation resolves the same external work and creates no duplicate state.
- A collect task receives the receipt document as a runtime input and passes the typed submission identity to
  `check_status`.
- A pending observation raises `RetryableError` from the collect task.
- A failed observation raises `TerminalError` from the collect task.
- A successful collection document records external observed content with `from_external_source`.
- The collection receipt is a causal source of the successful observation document.

## Child pipelines are scheduled by phase builder

A child pipeline is a pipeline boundary, not a service method and not a task helper. A parent phase schedules the child
pipeline through `f.child_pipeline`, using a child input bundle built from planning refs inside `Phase.build`. The
framework resolves those refs to concrete documents when the child boundary executes.

### Reference

```text
f.child_pipeline(
    ChildPipelineClass,
    *,
    run_config: ChildRunConfig,
    inputs: ChildInputs,
) -> ChildOutputs
```

### Constraints

- A parent phase schedules one child pipeline through `f.child_pipeline`.
- This is the one child-pipeline boundary an author writes, addressed by the child `Pipeline` class. Whether the
  child executes in-process or as a separately-deployed run — including invoking a deployed pipeline from outside the
  process — is a framework-owned placement decision, not a separate authored boundary; the placement-independent
  contract is `runtime-api/6-child-pipelines.md`.
- Task code does not invoke child pipelines directly.
- Parent configuration is mapped explicitly into the child run configuration.
- A parent phase outputs the child documents later parent phases may read.
- Application code does not branch on child pipeline placement.
- Child `RetryableError` and `TerminalError` propagate through `f.child_pipeline` to the parent phase boundary.
- A parent models expected child failure as a typed document outcome when that failure is part of normal business
  state.

### Invariants

- A child pipeline has its own input bundle, run configuration, output bundle, and run record.
- The child input bundle is the input bundle accepted by the child pipeline class.
- The child run configuration and delivered output bundle are the types declared by the child pipeline class.
