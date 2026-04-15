# AI Pipeline Core

Production framework for building type-safe AI pipelines — designed to be developed and used by AI coding agents. Open-sourced by [research.tech](https://research.tech).

[![Python Version](https://img.shields.io/badge/python-3.14%2B-blue)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Code Style: Ruff](https://img.shields.io/badge/code%20style-ruff-000000.svg)](https://github.com/astral-sh/ruff)
[![Type Checked: Basedpyright](https://img.shields.io/badge/type%20checked-basedpyright-blue)](https://github.com/DetachHead/basedpyright)

## Overview

AI Pipeline Core is a production-ready framework that combines document processing, LLM integration, and workflow orchestration into a unified system. Built with strong typing (Pydantic), automatic retries, cost tracking, and database-backed execution tracking, it enforces best practices while keeping application code minimal and straightforward.

This framework is the foundation of AI projects at [research.tech](https://research.tech). It is an internal-first solution, open-sourced because we believe in sharing production infrastructure publicly. The design prioritizes **strictness over flexibility** — all data structures are immutable, all inputs are validated at definition time, and all prompts are typed Python classes. These constraints exist because the framework is primarily developed and maintained by AI coding agents, which require rigid guardrails rather than flexible guidelines.

### Key Features

- **Document System**: Single `Document` base class with immutable content, SHA256-based identity, automatic MIME type detection, provenance tracking, multi-part attachments, and optional typed content via `Document[ModelType]`
- **Database Storage**: Unified database backends (ClickHouse production, filesystem CLI/download/replay, in-memory testing) with automatic deduplication
- **Conversation Class**: Immutable, stateful multi-turn LLM conversations with context caching, automatic URL/address shortening, and eager response restoration
- **LLM Integration**: Unified interface to any model via LiteLLM proxy (OpenRouter compatible) with context caching (default 300s TTL)
- **Tool Calling**: Define tools as typed Python classes with import-time validation, automatic schema generation, and a built-in auto-loop that executes tools and re-sends results until the LLM produces a final answer
- **Structured Output**: Type-safe generation with Pydantic model validation via `Conversation.send_structured()`
- **Workflow Orchestration**: Class-based `PipelineTask`, `PipelineFlow`, and `PipelineDeployment` with annotation-driven document types and import-time validation
- **Auto-Persistence**: `PipelineTask` saves returned documents to the active database backend automatically with provenance tracking
- **Image Processing**: Automatic image tiling/splitting for LLM vision models with model-specific presets
- **External Providers**: Base classes for integrating external services — `ExternalProvider` for synchronous HTTP APIs and `StatelessPollingProvider` for submit-then-poll patterns, with managed HTTP lifecycle, transport retries, exponential backoff, cost tracking, and ContextVar-based test overrides
- **Observability**: Database-backed execution DAGs, logs, replay payloads, and `ai-trace` download support
- **Prompt Compiler**: Type-safe prompt specifications replacing Jinja2 templates — typed Python classes for roles, rules, guides, and output formats with definition-time validation and a CLI tool for inspection
- **Replay**: Capture and re-execute any LLM conversation, pipeline task, or flow from recorded span JSON files or database-backed runs with document resolution via SHA256 references
- **Deployment**: Unified pipeline execution for local, CLI, and production environments with per-flow resume, idempotent remote Prefect submissions, and framework-specific remote error handling

## Installation

```bash
pip install ai-pipeline-core
```

This installs four CLI commands:
- `ai-prompt-compiler` — discover, inspect, render, and compile prompt specifications
- `ai-pipeline-deploy` — build and deploy pipelines to Prefect Cloud
- `ai-replay` — execute or inspect replayable span JSON files, or replay directly from database-backed runs
- `ai-trace` — list, show, and download execution data from the database

### Development Tools for Downstream Projects

Projects built on ai-pipeline-core can install the companion development tools directly from GitHub:

```bash
pip install "dev-cli @ git+https://github.com/researchtech-inc/ai-pipeline-core.git#subdirectory=tools/dev-cli"
pip install "docs-generator @ git+https://github.com/researchtech-inc/ai-pipeline-core.git#subdirectory=tools/docs-generator"
pip install "trace-inspector @ git+https://github.com/researchtech-inc/ai-pipeline-core.git#subdirectory=tools/trace-inspector"
```

Or add them to your project's `pyproject.toml`:

```toml
[project.optional-dependencies]
dev = [
    "dev-cli @ git+https://github.com/researchtech-inc/ai-pipeline-core.git#subdirectory=tools/dev-cli",
    "docs-generator @ git+https://github.com/researchtech-inc/ai-pipeline-core.git#subdirectory=tools/docs-generator",
    "trace-inspector @ git+https://github.com/researchtech-inc/ai-pipeline-core.git#subdirectory=tools/trace-inspector",
]
```

To pin to a specific version, append `@v0.22.2` (or a commit hash) before `#subdirectory`:

```toml
    "dev-cli @ git+https://github.com/researchtech-inc/ai-pipeline-core.git@v0.22.2#subdirectory=tools/dev-cli"
```

This installs three additional CLI commands:
- `dev` — development workflow CLI (test, lint, typecheck, format, check)
- `ai-docs` — AI-focused documentation generator
- `ai-trace-inspect` — trace inspection and markdown debug bundle generation

### Requirements

- Python 3.14 or higher
- Linux/macOS (Windows via WSL2)
- [uv](https://astral.sh/uv) (recommended)

### Versioning

This is an internal framework under active development. **No backward compatibility is guaranteed between versions** — pin your dependency to an exact version. There is no changelog; the git commit history serves as the changelog.

### Crash Visibility

Near-zero tail loss on SIGKILL is the right expectation for stdout logging, not zero-loss durability. The platform logging agent still buffers recent log lines locally before shipping them, so an OOM or SIGKILL can drop the very end of the stream. That is still materially better than deferred database log flushing, but truly zero-loss logging would require synchronous network writes on every log line, which is too expensive for the framework's hot path.

### Development Installation

```bash
git clone https://github.com/researchtech-inc/ai-pipeline-core.git
cd ai-pipeline-core
make install-dev     # Initializes uv environment and installs pre-commit hooks
```

## Quick Start

### Basic Pipeline

```python
from pydantic import BaseModel, Field

from ai_pipeline_core import (
    Document,
    DeploymentResult,
    FlowOptions,
    PipelineDeployment,
    PipelineFlow,
    PipelineTask,
)

import logging

logger = logging.getLogger(__name__)


# 1. Define document types (subclass Document)
class InputDocument(Document):
    """Pipeline input."""


class AnalysisDocument(Document):
    """Per-document analysis result."""


class ReportDocument(Document):
    """Final compiled report."""


# 2. Structured output model
class AnalysisSummary(BaseModel):
    word_count: int
    top_keywords: list[str] = Field(default_factory=list)


# 3. Pipeline task -- class-based, auto-saves returned documents to the active database backend
class AnalyzeDocument(PipelineTask):
    """Analyze a single document."""

    @classmethod
    async def run(cls, documents: tuple[InputDocument, ...]) -> tuple[AnalysisDocument, ...]:

        doc = documents[0]
        return (
            AnalysisDocument.derive(
                name=f"analysis_{doc.sha256[:12]}.json",
                content=AnalysisSummary(word_count=42, top_keywords=["ai", "pipeline"]),
                derived_from=(doc,),
            ),
        )


# 4. Pipeline flow -- type contract is in the run() annotations
class AnalysisFlow(PipelineFlow):
    """Analyze all input documents."""

    async def run(self, input_documents: tuple[InputDocument, ...], options: FlowOptions) -> tuple[AnalysisDocument, ...]:
        results: list[AnalysisDocument] = []
        for doc in input_documents:
            results.extend(await AnalyzeDocument.run(documents=(doc,)))
        return tuple(results)


class ReportFlow(PipelineFlow):
    """Generate final report from analyses."""

    async def run(self, analyses: tuple[AnalysisDocument, ...], options: FlowOptions) -> tuple[ReportDocument, ...]:
        report = ReportDocument.derive(
            name="report.md",
            content="# Report\n\nAnalysis complete.",
            derived_from=analyses,
        )
        return (report,)


# 5. Deployment -- build the execution plan up front
class MyResult(DeploymentResult):
    report_count: int = 0


class MyPipeline(PipelineDeployment[FlowOptions, MyResult]):
    def build_plan(self, options: FlowOptions) -> DeploymentPlan:
        return DeploymentPlan(
            steps=(
                FlowStep(AnalysisFlow()),
                FlowStep(ReportFlow()),
            )
        )

    @staticmethod
    def build_result(
        run_id: str,
        documents: tuple[Document, ...],
        options: FlowOptions,
    ) -> MyResult:
        reports = [d for d in documents if isinstance(d, ReportDocument)]
        return MyResult(success=True, report_count=len(reports))


# 6. CLI initializer provides run ID and initial documents
def initialize(options: FlowOptions) -> tuple[str, tuple[Document, ...]]:
    docs: tuple[Document, ...] = (InputDocument.create_root(name="input.txt", content="Sample data", reason="CLI input"),)
    return "my-project", docs


# Run from CLI (requires positional working_directory arg: python script.py ./output)
pipeline = MyPipeline()
pipeline.run_cli(initializer=initialize)
```

### Conversation (Multi-Turn LLM)

```python
from ai_pipeline_core.llm import Conversation, ModelOptions

# Create a conversation with model and optional context
conv = Conversation(model="gemini-3.1-pro")

# Add documents to cacheable context prefix (shared across forks)
conv = conv.with_context(doc1, doc2, doc3)

# Add a document to dynamic messages suffix (NOT cached, per-fork content)
conv = conv.with_document(my_document)

# Send a message (returns NEW immutable Conversation instance — always capture!)
conv = await conv.send("Analyze the document")
print(conv.content)  # Response text

# Structured output
conv = await conv.send_structured("Extract key points", response_format=KeyPoints)
print(conv.parsed)  # KeyPoints instance

# Multi-turn: each send() appends to conversation history
conv = await conv.send("Now summarize the key points")
print(conv.content)

# Access response properties
print(conv.reasoning_content)  # Thinking/reasoning text (if available)
print(conv.usage)  # Token usage with input/output counts
print(conv.cost)  # Estimated cost
print(conv.citations)  # Citation objects (for search models)
```

### Tool Calling

```python
from pydantic import BaseModel, Field
from ai_pipeline_core import Conversation, Tool


# 1. Define a tool — docstring becomes the LLM description
class GetWeather(Tool):
    """Get current weather for a city."""

    class Input(BaseModel):
        city: str = Field(description="City name")
        unit: str = Field(default="celsius", description="Temperature unit")

    class Output(BaseModel):
        weather: str

    async def run(self, input: Input) -> Output:
        # Call your API, database, or any async operation here
        return self.Output(weather=f"Sunny, 22°C in {input.city}")


# 2. Pass tools to send() — auto-loop handles everything
conv = Conversation(model="gemini-3-flash")
conv = await conv.send(
    "What's the weather in Paris?",
    tools=[GetWeather()],
)
print(conv.content)  # "It's sunny and 22°C in Paris!"

# 3. Inspect what tools were called
for record in conv.tool_call_records:
    print(f"Tool: {record.tool.__name__}, Round: {record.round}")
    print(f"Input: {record.input}")
    print(f"Output JSON: {record.output.content}")
    print(f"Output model: {record.output.data}")
```

**How the auto-loop works:** `send()` calls the LLM → if the LLM requests tool calls, the framework executes them in parallel → sends results back → repeats until the LLM produces a final text answer or `max_tool_rounds` is exhausted.

**Tool definition rules (validated at import time):**
- Must have a non-empty docstring (becomes the tool description for the LLM)
- Must define an `Input` inner class (BaseModel with `Field(description=...)` on every field)
- Must define an `Output` inner class (BaseModel)
- Must define an `async def run(self, input: Input) -> Output` method
- `execute()` is sealed and framework-owned; do not override it (it handles retry, timeout, error handling, and serialization)
- Lifecycle behavior is configurable through ClassVars: `retries`, `retry_delay_seconds`, `timeout_seconds`, `max_response_bytes`, `handled_exceptions`
- `dict[str, V]` field types are forbidden in `Input` (OpenAI strict mode incompatible)
- Field names `strict` and `additionalProperties` are reserved and cannot be used (collide with LiteLLM's recursive key stripping)

**Tool naming:** Class names are auto-converted to snake_case for the LLM (`GetWeather` → `get_weather`). Duplicate names after conversion raise `ValueError`.

**Additional options:**
- `tool_choice="required"` — force the LLM to call a tool on the first round
- `tool_choice="none"` — prevent tool calls (useful for final summarization)
- `max_tool_rounds=N` — limit the number of tool call rounds (default 10)
- Tools work with both `send()` and `send_structured()` — structured output is produced on the final response

### Structured Output

```python
from pydantic import BaseModel
from ai_pipeline_core import Conversation


class Analysis(BaseModel):
    summary: str
    sentiment: float
    key_points: list[str]


# Generate structured output via Conversation
conv = Conversation(model="gemini-3.1-pro")
conv = await conv.send_structured(
    "Analyze this product review: ...",
    response_format=Analysis,
)

# Access parsed result with type safety
analysis = conv.parsed
print(f"Sentiment: {analysis.sentiment}")
for point in analysis.key_points:
    print(f"- {point}")
```

### Document Handling

```python
from ai_pipeline_core import Document


class MyDocument(Document):
    """Custom document type -- must subclass Document."""


# Create documents from external sources (URI provenance)
doc = MyDocument.create_external(
    name="data.json",
    content={"key": "value"},  # Automatically converted to JSON bytes
    from_sources=["https://api.example.com/data"],
)

# Parse back to original type
data = doc.parse(dict)  # Returns {"key": "value"}

# Document provenance tracking
source_doc = MyDocument.create_root(name="source.txt", content="original data", reason="user upload")
plan_doc = MyDocument.derive(name="plan.txt", content="research plan", derived_from=(source_doc,))
derived = MyDocument.create_external(
    name="derived.json",
    content={"result": "processed"},
    from_sources=["https://api.example.com/data"],  # Content came from this URL
    triggered_by=(plan_doc,),  # Created because of this plan (causal, not content)
)

# Check provenance
for hash in derived.content_documents:
    print(f"Derived from document: {hash}")
for ref in derived.content_references:
    print(f"External source: {ref}")
```

### Typed Content (Document[T])

Declare a Pydantic BaseModel as the content schema for a Document subclass. Content is validated at creation time and accessible via `.parsed`:

```python
from pydantic import BaseModel
from ai_pipeline_core import Document


class ResearchDefinition(BaseModel, frozen=True):
    topic: str
    max_sources: int = 10


class ResearchPlanDocument(Document[ResearchDefinition]):
    """Plan document with typed content schema."""


# Content is validated against the schema at creation time
plan = ResearchPlanDocument.derive(
    name="plan.json",
    content=ResearchDefinition(topic="AI safety"),
    derived_from=(input_doc,),
)

# Zero-boilerplate typed access (cached, returns ResearchDefinition)
definition = plan.parsed
print(definition.topic)  # "AI safety"
print(definition.max_sources)  # 10


# Wrong schema type is rejected at creation time
class WrongModel(BaseModel, frozen=True):
    x: int


plan = ResearchPlanDocument.derive(
    name="plan.json",
    content=WrongModel(x=1),  # TypeError: Expected content of type ResearchDefinition
    derived_from=(input_doc,),
)

# Introspection
ResearchPlanDocument.get_content_type()  # ResearchDefinition
```

List content is also supported — `Document[list[ModelType]]` validates each item:

```python
class FindingItem(BaseModel, frozen=True):
    title: str
    severity: str


class FindingsDocument(Document[list[FindingItem]]):
    """Document containing a list of findings."""


doc = FindingsDocument.create_root(
    name="findings.json",
    content=[FindingItem(title="XSS", severity="high")],
    reason="audit output",
)
doc.parsed  # list[FindingItem]
doc.content_is_list()  # True
```

**Co-location rule:** The content model must be defined in the same module as the Document subclass. Cross-module content models raise `TypeError` at import time.

## Core Concepts

### Documents

Documents are immutable Pydantic models that wrap binary content with metadata. There is a single `Document` base class -- subclass it to define your document types:

```python
class MyDocument(Document):
    """All documents subclass Document directly."""


# Use derive() for content transformations
doc = MyDocument.derive(
    name="data.json",
    content={"key": "value"},  # Auto-converts to JSON
    derived_from=(source,),
)

# Access content
if doc.is_text:
    print(doc.text)

# Parse structured data
data = doc.as_json()  # or as_yaml()
model = doc.as_pydantic_model(MyModel)  # Requires model_type argument

# Content-addressed identity
print(doc.sha256)  # Full SHA256 hash (base32)
print(doc.id)  # Short 6-char identifier
```

**Typed content** — declare a content schema via generic parameter for automatic validation and typed access:

```python
class PlanDocument(Document[PlanModel]):
    """Content is validated against PlanModel at creation time."""


plan = PlanDocument.derive(name="plan.json", content=PlanModel(...), derived_from=(source_doc,))
plan.parsed  # → PlanModel (cached, typed)
PlanDocument.get_content_type()  # → PlanModel
```

**Document fields:**
- `name`: Filename (validated for security -- no path traversal)
- `description`: Optional human-readable description
- `content`: Raw bytes (auto-converted from str, dict, list, BaseModel via `create()`)
- `derived_from`: Content provenance — SHA256 hashes of source documents (stored internally). Constructors (`derive()`, `create()`) accept `Sequence[Document]` and extract hashes automatically. URI-style references use `create_external(from_sources=...)` instead.
- `triggered_by`: Causal provenance — SHA256 hashes of documents that caused this document to be created without contributing to its content. Constructors accept `Sequence[Document]` and extract hashes automatically.
- `attachments`: Tuple of `Attachment` objects for multi-part content

Documents support:
- Automatic content serialization based on file extension: `.json` → JSON, `.yaml`/`.yml` → YAML, others → UTF-8 text. Structured data (dict, list, BaseModel) requires `.json` or `.yaml` extension.
- Optional typed content schema via `Document[ModelType]` with creation-time validation and `.parsed` access
- MIME type detection via `mime_type` cached property, with `is_text`/`is_image`/`is_pdf` helpers
- SHA256-based identity and deduplication
- Provenance tracking (`derived_from` for content sources, `triggered_by` for causal lineage)
- `FILES` enum for filename restrictions (definition-time validation)
- Four construction paths: `create_root()` (pipeline inputs), `derive(derived_from=...)` (content transformations), `create(triggered_by=...)` (causal provenance), `create_external(from_sources=...)` (URI provenance). All accept `Sequence[Document]` and extract SHA256 hashes automatically
- Token count estimation via `approximate_tokens_count`

### Database-backed Persistence

Documents are automatically persisted by `PipelineTask` to the active database backend. Application code typically reads through `DatabaseReader`; write operations stay framework-internal.

**Backend implementations** (internal, auto-selected by execution mode):
- **ClickHouseDatabase**: Production backend
- **FilesystemDatabase**: CLI, download, and replay-friendly filesystem snapshot backend
- **MemoryDatabase**: Testing and local in-memory execution

**Backend selection depends on the execution mode:**
- `run_cli()`: Uses `FilesystemDatabase` by default, or `ClickHouseDatabase` when ClickHouse is configured
- `run_local()`: Uses `MemoryDatabase`
- `as_prefect_flow()`: Auto-selects the configured production backend

**Public API — `DatabaseReader`** (read-only protocol):
- `get_span(span_id)` — Retrieve a span by its ID
- `get_child_spans(parent_span_id)` — Retrieve direct child spans ordered by sequence number
- `get_document(document_sha256)` — Load a document record by SHA256
- `get_documents_batch(sha256s)` — Retrieve multiple document records keyed by SHA256
- `get_document_with_content(document_sha256)` — Load document metadata plus content and attachment blobs
- `find_documents_by_name(names, document_type)` — Find document records by exact name match (takes `list[str]`)
- `get_deployment_tree(deployment_id)` — Retrieve every span in a deployment tree as a flat list
- `get_deployment_by_run_id(run_id)` — Find the newest deployment span for a run ID
- `get_span_logs(span_id)` / `get_deployment_logs(deployment_id)` — Retrieve execution logs
- `list_deployments(limit, status)` — List deployment spans ordered by newest first
- `get_cached_completion(cache_key, max_age)` — Find a cached completed span within the max age window
- `get_deployment_cost_totals(deployment_id)` — Aggregate cost and token totals for a deployment tree
- `get_blob(content_sha256)` / `get_blobs_batch(content_sha256s)` — Retrieve blob content by SHA256
- Replay and download helpers resolve document/blob content through the same interface

Write operations (`insert_span`, `save_document`, `save_blob`, `save_document_batch`, `save_blob_batch`, `save_logs_batch`, `flush`, `shutdown`) are framework-internal — the framework handles persistence automatically. `DatabaseWriter` exposes a `supports_remote` property indicating whether the backend supports Prefect-based remote deployment execution.

**Document summaries:** Persisted `summary` storage is supported via `Document.create(..., summary=...)` and `update_document_summary()`. Summaries are stored as metadata on document records. Configure via `DOC_SUMMARY_ENABLED` and `DOC_SUMMARY_MODEL`.

### LLM Integration

The primary interface is the **`Conversation`** class for multi-turn interactions.

#### Conversation (Recommended)

The `Conversation` class provides immutable, stateful conversation management:

```python
from ai_pipeline_core.llm import Conversation, ModelOptions

# Create with model and optional configuration
conv = Conversation(model="gemini-3.1-pro")

# Add documents to cacheable context prefix (shared across forks)
conv = conv.with_context(doc1, doc2, doc3)

# Add a document to dynamic messages suffix (NOT cached, per-fork content)
conv = conv.with_document(my_document)

# Configure model options
conv = conv.with_model_options(
    ModelOptions(
        system_prompt="You are a research analyst.",
        reasoning_effort="high",
    )
)

# Send a message (returns NEW Conversation instance)
conv = await conv.send("Analyze the document")
print(conv.content)  # Response text

# Multi-turn: conversation history is preserved
conv = await conv.send("Now summarize the key points")
print(conv.content)

# Structured output — single model
conv = await conv.send_structured("Extract entities", response_format=Entities)
print(conv.parsed)  # Entities instance

# Structured output — list of models (auto-wrapped for provider compatibility)
conv = await conv.send_structured("Extract all findings", response_format=list[Finding])
print(conv.parsed)  # list[Finding] — framework wraps/unwraps transparently

# Add multiple documents at once
conv = conv.with_documents([doc1, doc2, doc3])

# Inject prior assistant output (e.g., from another conversation)
conv = conv.with_assistant_message("Previous analysis result...")

# Approximate token count for all context and messages
print(conv.approximate_tokens_count)

# Tool calling — LLM can call tools, framework auto-loops
conv = await conv.send("Search for recent news", tools=[SearchTool()])
print(conv.content)  # Final answer after tool execution
print(conv.tool_call_records)  # Records of all tool calls made
```

**`send_spec()`** — sends a `PromptSpec` to the LLM. Handles document placement, stop sequences, and auto-extraction of `<result>` tags. For structured specs (`PromptSpec[SomeModel]`), dispatches to `send_structured()` automatically.

**Content protection (automatic):** URLs, blockchain addresses, and high-entropy strings in context documents are automatically shortened to `prefix...suffix` forms to save tokens. Both `.content` and `.parsed` are eagerly restored after every `send()`/`send_structured()` call — no manual restoration needed. A fuzzy fallback handles LLM-mangled forms (dropped suffix, prefix/suffix truncated by 1-2 chars).

**`ModelOptions` key fields (all optional with sensible defaults):**
- `cache_ttl`: Context cache TTL (default `"300s"`, set `None` to disable)
- `system_prompt`: System-level instructions
- `reasoning_effort`: `"low" | "medium" | "high"` for models with explicit reasoning
- `search_context_size`: `"low" | "medium" | "high"` for search-enabled models
- `retries`: Retry attempts (default `None` → uses `Settings.conversation_retries`, default `3`)
- `retry_delay_seconds`: Flat delay override between retries (default `None` → otherwise uses exponential backoff from `Settings.conversation_retry_delay_seconds=30`, `conversation_retry_backoff_multiplier=3`, capped at `conversation_retry_max_delay_seconds=300`)
- `cache_warmup_max_wait`: Max seconds followers wait for warmup cache (default `None` = disabled, recommended `600.0`)
- `cache_warmup_max_qps`: Per-prefix QPS limit for warmup (default `None` = no limit, recommended `15` for Gemini)
- `timeout`: Max wait seconds (default `600`)
- `service_tier`: `"auto" | "default" | "flex" | "scale" | "priority"` (OpenAI only)
- `max_completion_tokens`: Max output tokens
- `temperature`: Generation randomness (usually omit -- use provider defaults)
- `stop`: Stop sequences (tuple of strings, used internally by `send_spec` for `</result>` tags)

**ModelName predefined values:** `"gemini-3.1-pro"`, `"gpt-5.4"`, `"gemini-3-flash"`, `"gpt-5.4-mini"`, `"grok-4.1-fast"`, `"gemini-3-flash-search"`, `"gpt-5.4-mini-search"`, `"grok-4.1-fast-search"`, `"sonar-pro-search"` (also accepts any string for custom models).

### Image Processing

Image processing for LLM vision models is available from the `llm._images` module:

```python
from ai_pipeline_core.llm._images import process_image, ImagePreset

# Process an image with model-specific presets
result = process_image(screenshot_bytes, preset=ImagePreset.GEMINI)
for part in result:
    print(part.label, len(part.data))
```

Available presets: `GEMINI` (3000px, 9M pixels), `CLAUDE` (1568px, 1.15M pixels), `GPT4V` (2048px, 4M pixels), `DEFAULT` (1000px, 1M pixels).

**Token cost:** A single image is estimated at **1080 tokens** for token counting purposes (actual usage depends on provider).

The `Conversation` class automatically splits oversized images when documents are added to context — you typically don't need to call `process_image` directly.

### Exceptions

The framework re-exports key exceptions at the top level for convenient catching:

```python
from ai_pipeline_core import PipelineCoreError, NonRetriableError, LLMError, EmptyResponseError, DocumentValidationError, DocumentSizeError, DocumentNameError
```

- `PipelineCoreError` — Base for all framework exceptions
- `NonRetriableError` — Signals that an operation must not be retried (stops task/flow retry loops immediately)
- `LLMError` — LLM generation failures (retries exhausted, timeouts, degeneration)
- `EmptyResponseError` — Blank/empty LLM response (subclass of `LLMError`, triggers retry with cache disabled)
- `ProviderError` — External provider call failed after all retries (subclass of `PipelineCoreError`)
- `ProviderAuthError` — Authentication/authorization failure (401/403), never retried (subclass of both `ProviderError` and `NonRetriableError`)
- `DocumentValidationError` — Document validation failures
- `DocumentSizeError` — Document exceeds size limits
- `DocumentNameError` — Invalid document name (path traversal, etc.)

Output degeneration (token repetition loops) is detected automatically and raises `LLMError` after retry exhaustion.

### Pipeline Classes

The pipeline system uses a three-tier class hierarchy: `PipelineTask` → `PipelineFlow` → `PipelineDeployment`. All classes use `__init_subclass__` for import-time validation — errors are caught when the module is imported, not at runtime.

#### `PipelineTask`

Base class for pipeline tasks with automatic execution-node tracking, document persistence, and lifecycle events:

```python
from ai_pipeline_core import PipelineTask


class ProcessChunk(PipelineTask):
    """Process a single document chunk."""

    @classmethod
    async def run(cls, documents: tuple[InputDocument, ...]) -> tuple[OutputDocument, ...]:

        doc = documents[0]
        return (
            OutputDocument.derive(
                name="result.json",
                content={"processed": True},
                derived_from=(doc,),
            ),
        )


class ExpensiveTask(PipelineTask):
    retries = 3
    timeout_seconds = 600
    estimated_minutes = 5

    @classmethod
    async def run(cls, documents: tuple[InputDocument, ...]) -> tuple[OutputDocument, ...]: ...
```

**Invocation patterns:**

```python
# Sequential — await TaskClass.run(...)
result = await ProcessChunk.run(documents)

# Parallel — TaskClass.run() without await returns TaskHandle
handle = ProcessChunk.run(documents)
result = await handle.result()
```

**ClassVar configuration:**
- `retries`: Retry attempts on failure (default `None` → uses `Settings.task_retries`, default `2`; exponential backoff)
- `retry_delay_seconds`: Base delay between retries (default `None` → uses `Settings.task_retry_delay_seconds`, default `30`; doubles each attempt, capped at 300s)
- `timeout_seconds`: Task execution timeout (default `None`)
- `estimated_minutes`: Duration estimate for progress tracking (default `1`, must be >= 1)
- `expected_cost`: Expected cost budget for cost tracking

**Key features:**
- Import-time validation of `run()` signature and document type annotations
- Async-only enforcement (raises `TypeError` if `run` is not `async def`)
- Rejects classes starting with `Test` (reserved for pytest)
- Rejects required `__init__` parameters (tasks use documents-only invocation)
- Automatic execution-node tracking
- Document auto-save to the active database backend (returned documents are persisted)
- Source validation (warns if referenced SHA256s don't exist in the database)
- Task-level lifecycle events (`TaskStartedEvent`, `TaskCompletedEvent`, `TaskFailedEvent`)

#### `PipelineFlow`

Base class for pipeline flows that orchestrate tasks:

```python
from ai_pipeline_core import PipelineFlow, FlowOptions


class AnalysisFlow(PipelineFlow):
    """Analyze input documents."""

    async def run(
        self,
        input_docs: tuple[InputDoc, ...],  # Collection input: all matching docs
        options: MyFlowOptions,  # Must be FlowOptions or subclass
    ) -> tuple[OutputDoc, ...]:  # Output types extracted from annotation
        results: list[OutputDoc] = []
        for doc in input_docs:
            results.extend(await AnalyzeTask.run(documents=(doc,)))
        return tuple(results)
```

Flow input parameter annotations determine input resolution from the blackboard:
- `source: MyDocument` means the latest matching document is injected
- `source: MyDocument | None` means latest-or-`None`
- `sources: tuple[MyDocument, ...]` means all matching documents
- `options: MyFlowOptions` receives the deployment options object

The flow return annotation determines output types. `run()` must be an instance method with `self` first, at least one named input parameter, and a `tuple[DocumentSubclass, ...]` return annotation. Use `get_run_id()` inside a flow when you need the current run ID.

**Constructor parameters** for per-instance configuration:

```python
class ConfigurableFlow(PipelineFlow):
    """Flow with per-instance model configuration."""

    async def run(self, input_docs: tuple[InputDoc, ...], options: FlowOptions) -> tuple[OutputDoc, ...]:
        _ = (input_docs, options)
        model = self.model  # Access constructor params as attributes
        ...


flow = ConfigurableFlow(model="gemini-3.1-pro", temperature=0.7)
flow.get_params()  # {"model": "gemini-3.1-pro", "temperature": 0.7}
```

**Flow ClassVar configuration:**
- `retries`: Per-flow retry override (default `None` — defers to deployment's `flow_retries`, then to `Settings.flow_retries`, default `0`)
- `retry_delay_seconds`: Per-flow delay override (default `None` — defers to deployment, then `Settings.flow_retry_delay_seconds`, default `30`)
- `estimated_minutes`: Duration estimate for progress tracking (default `1`, must be >= 1)

Flow retries are controlled by the deployment (see `flow_retries` on `PipelineDeployment`). A flow can override with an explicit `retries = N` in its class body. Raise `NonRetriableError` to stop retries immediately.

**FlowOptions** is a frozen `BaseModel` for pipeline configuration. Subclass it to add flow-specific parameters:

```python
class ResearchOptions(FlowOptions):
    analysis_model: ModelName = "gemini-3.1-pro"
    verification_model: ModelName = "grok-4.1-fast"
    synthesis_model: ModelName = "gemini-3.1-pro"
    max_sources: int = 10
```

#### `PipelineDeployment`

Orchestrates multi-flow pipelines with resume, per-flow uploads, and event publishing:

```python
class MyPipeline(PipelineDeployment[MyOptions, MyResult]):
    pubsub_service_type = "research"  # Enables Pub/Sub event publishing

    def build_plan(self, options: MyOptions) -> DeploymentPlan:
        return DeploymentPlan(
            steps=(
                FlowStep(AnalysisFlow()),
                FlowStep(ReportFlow(model="gemini-3.1-pro")),
            )
        )

    @staticmethod
    def build_result(
        run_id: str,
        documents: tuple[Document, ...],
        options: MyOptions,
    ) -> MyResult: ...
```

**Dynamic flow control** with `build_plan()` and `FieldGate`:

```python
from pydantic import BaseModel
from ai_pipeline_core import DeploymentPlan, Document, FieldGate, FlowStep


class SynthesisDecision(BaseModel, frozen=True):
    should_synthesize: bool


class SynthesisDecisionDocument(Document[SynthesisDecision]):
    pass


class MyPipeline(PipelineDeployment[MyOptions, MyResult]):
    def build_plan(self, options: MyOptions) -> DeploymentPlan:
        return DeploymentPlan(
            steps=(
                FlowStep(ExtractFlow()),
                FlowStep(AnalyzeFlow()),
                FlowStep(
                    SynthesisFlow(),
                    run_if=FieldGate(SynthesisDecisionDocument, "should_synthesize", op="truthy", on_missing="skip"),
                ),
            )
        )
```

**Execution modes:**

```python
pipeline = MyPipeline()

# CLI mode: parses sys.argv, requires positional working_directory argument
# Usage: python script.py ./output [--start N] [--end N] [--max-keywords 8]
pipeline.run_cli(initializer=init_fn)

# Local mode: in-memory store, returns result directly (synchronous)
result = pipeline.run_local(
    run_id="test",
    documents=input_docs,
    options=MyOptions(),
)

# Production: generates a Prefect flow for deployment
prefect_flow = pipeline.as_prefect_flow()
```

**Features:**
- **Per-flow resume**: Skips flows with a cached completed execution node in the database (explicit completion tracking, not document-presence inference). Configurable `cache_ttl` (default 24h)
- **Type chain validation**: At runtime, validates that at least one of each flow's declared input types is producible by preceding flows (union semantics)
- **Event publishing**: 11 lifecycle events (run started/completed/failed, flow started/completed/failed/skipped, task started/completed/failed, heartbeat) via Pub/Sub. Enabled by setting `pubsub_service_type` ClassVar. Requires `PUBSUB_PROJECT_ID` and `PUBSUB_TOPIC_ID` env vars
- **Dynamic flow control**: `build_plan()` returns `FlowStep` entries, and `FieldGate` / `group_stop_if` drive runtime skips and loop stops from typed control documents
- **Flow retries**: Configurable via `flow_retries` (default `None` → uses `Settings.flow_retries`, default `0`) and `flow_retry_delay_seconds` (default `None` → uses `Settings.flow_retry_delay_seconds`, default `30`) ClassVars on the deployment. Exponential backoff, capped at 300s. Individual flows can override with explicit `retries = N`
- **Concurrency limits**: Cross-run enforcement via Prefect global concurrency limits
- **CLI mode**: `--start N` / `--end N` for step control with the configured database backend

#### Concurrency Limits

Declare cross-run concurrency and rate limits on `PipelineDeployment` to prevent exceeding external API quotas across all concurrent pipeline runs:

```python
from ai_pipeline_core import LimitKind, PipelineLimit, PipelineDeployment, pipeline_concurrency


class MyPipeline(PipelineDeployment[MyOptions, MyResult]):
    concurrency_limits = {
        "provider-a": PipelineLimit(500, LimitKind.CONCURRENT),  # max 500 simultaneous
        "provider-b": PipelineLimit(15, LimitKind.PER_MINUTE, timeout=300),  # 15/min token bucket
    }
    ...
```

Use `pipeline_concurrency()` at call sites to acquire slots:

```python
from ai_pipeline_core import pipeline_concurrency


async def fetch_data(url: str) -> Data:
    async with pipeline_concurrency("provider-a"):
        return await provider.fetch(url)
```

**Limit kinds:**
- `CONCURRENT` — Lease-based slots held during operation, released on exit
- `PER_MINUTE` — Token bucket with `limit/60` decay per second (allows bursting)
- `PER_HOUR` — Token bucket with `limit/3600` decay per second

**Behavior:**
- Limits are auto-created in Prefect server at pipeline start (idempotent upsert)
- Timeout raises `AcquireConcurrencySlotTimeoutError` (limit doing its job)
- When Prefect is unavailable, limits proceed unthrottled (logged as warning)
- Limit names are validated at class definition time (alphanumeric, dashes, underscores)

#### Stub Classes for Incremental Development

Use `stub=True` to define placeholder classes with correct type signatures that pass all validation but block deployment. This enables multi-agent development where an architect defines the pipeline structure and individual agents implement pieces incrementally:

```python
from ai_pipeline_core import PipelineTask, PipelineFlow, PromptSpec, FlowOptions


# Stub task — type contract defined, implementation pending
class AnalyzeDataTask(PipelineTask, stub=True):
    """Analyze cleaned data and produce structured findings.

    Input: CleanedDataDoc with normalized content.
    Output: AnalysisResultDoc with analysis findings.
    """

    @classmethod
    async def run(cls, cleaned_docs: tuple[CleanedDataDoc, ...]) -> tuple[AnalysisResultDoc, ...]: ...


# Stub flow — same pattern
class AnalysisFlow(PipelineFlow, stub=True):
    """Run analysis pipeline."""

    async def run(self, cleaned_docs: tuple[CleanedDataDoc, ...], options: FlowOptions) -> tuple[AnalysisResultDoc, ...]: ...


# Stub PromptSpec — uses keyword arg syntax
class AnalysisSpec(PromptSpec, stub=True):
    """Analyze data for themes and anomalies."""

    role = AnalystRole
    input_documents = (CleanedDataDoc,)
    task = "Analyze the provided data for key themes and anomalies."
```

**Behavior:**
- Stubs pass all definition-time validation (ruff, basedpyright, semgrep, import-time checks)
- Type contracts are fully preserved — `input_document_types` and `output_document_types` are extracted from annotations
- `_validate_flow_chain` works normally with stub flows (type chain is verified)
- Executing a stub raises `StubNotImplementedError` (subclass of `NonRetriableError` — no retries)
- `deploy.py` blocks deployment if any stubs exist in the import graph
- To implement: fill in the `run()` body and remove `stub=True` from the class declaration

See `examples/showcase_stubs.py` for a complete working example.

#### Parallel Execution

**Task dispatch** for parallel task execution within flows:

```python
from ai_pipeline_core import PipelineTask, collect_tasks, as_task_completed

# Dispatch tasks for parallel execution (run without await returns TaskHandle)
handle_a = ExtractTask.run(docs_a)
handle_b = ExtractTask.run(docs_b)
handle_c = ExtractTask.run(docs_c)

# Collect all results with optional deadline
batch = await collect_tasks(handle_a, handle_b, handle_c, deadline_seconds=120.0)
# batch.completed = [result_a, result_b, ...]
# batch.incomplete = [handle_c]  # timed out or failed

# Iterate in completion order
async for handle in as_task_completed(handle_a, handle_b, handle_c):
    result = await handle.result()
```

**`safe_gather` and `safe_gather_indexed`** run coroutines in parallel with fault tolerance:

```python
from ai_pipeline_core import safe_gather, safe_gather_indexed

# safe_gather: returns successes only, filters out failures
results = await safe_gather(
    process(doc1),
    process(doc2),
    process(doc3),
    label="processing",
)  # Returns list of successful results (order may shift)

# safe_gather_indexed: preserves positional correspondence (None for failures)
results = await safe_gather_indexed(
    process(doc1),
    process(doc2),
    process(doc3),
    label="processing",
)  # Returns [result1, None, result3] if doc2 failed
```

Both raise if all coroutines fail (configurable via `raise_if_all_fail=False`).

#### Deploying to Prefect Cloud

The framework includes a deploy script that builds a fully bundled deployment (project wheel + all dependency wheels), uploads to GCS, and creates a Prefect deployment. The worker installs fully offline with `--no-index` — no PyPI contact, no stale cache issues.

```bash
# From your project root (where pyproject.toml lives)
ai-pipeline-deploy

# Also available as module:
python -m ai_pipeline_core.deployment.deploy
```

**Requirements:**
- `uv` (dependency resolution) and `pip` (wheel download) on the deploy machine
- `PREFECT_API_URL`, `PREFECT_GCS_BUCKET` configured
- `uv` on the worker (for offline install)

#### Remote Deployment Client

`RemoteDeployment` is a typed client for calling a remote `PipelineDeployment` via Prefect. Name the client class identically to the server's deployment class so the auto-derived deployment name matches:

```python
from ai_pipeline_core import RemoteDeployment, DeploymentResult, FlowOptions, Document


class RemoteInputDocument(Document):
    """Mirror type -- class_name must match the remote pipeline's document type."""


class RemoteResult(DeploymentResult):
    """Result type matching the remote pipeline's result."""

    report_count: int = 0


class MyPipeline(RemoteDeployment[FlowOptions, RemoteResult]):
    """Client for the remote MyPipeline deployment."""


client = MyPipeline()
result = await client.run(
    documents=input_docs,
    options=FlowOptions(),
)
```

The client defines local Document subclasses ("mirror types") whose `class_name` must match the remote pipeline's document types exactly. `run_remote_deployment()` is also available as a lower-level function. Remote execution only happens when the active database backend reports `supports_remote=True`; otherwise it falls back to inline execution.

**Deterministic run_id**: `RemoteDeployment` derives a deterministic `run_id` from the caller's run_id + a combined fingerprint hash of all document SHA256s and serialized options (format: `{run_id}-{fingerprint[:8]}`). Same inputs always produce the same derived run_id, enabling worker-side flow resume.

**run_id validation**: All `run_id` values are validated at entry points — alphanumeric characters, underscores, and hyphens only, max 100 characters.

### Prompt Compiler

Type-safe prompt specifications that replace Jinja2 templates. Every piece of prompt content is a class or class attribute, validated at definition time (import time).

**Components** — define once, reuse across specs:

```python
from ai_pipeline_core import Role, Rule, OutputRule, Guide


class ResearchAnalyst(Role):
    """Analyst role for research pipelines."""

    text = "experienced research analyst with expertise in data synthesis"


class CiteEvidence(Rule):
    """Citation rule."""

    text = "Always cite specific evidence from the source documents.\nInclude document IDs when referencing."


class DontUseMarkdownTables(OutputRule):
    """Table formatting rule."""

    text = "Do not use markdown tables in the output."


class RiskFrameworkGuide(Guide):
    """Risk assessment framework guide."""

    template = "guides/risk_framework.md"  # Relative to module file, loaded at import time
```

**Specs** — typed prompt definitions with full validation:

```python
from ai_pipeline_core import PromptSpec, Document
from pydantic import Field


class SourceDocument(Document):
    """Source material for analysis."""


class AnalysisSpec(PromptSpec):
    """Analyze source documents for key findings."""

    role = ResearchAnalyst
    input_documents = (SourceDocument,)
    task = "Analyze the provided documents and identify key findings."
    rules = (CiteEvidence,)
    guides = (RiskFrameworkGuide,)
    output_structure = "## Key Findings\n## Evidence\n## Gaps"
    output_rules = (DontUseMarkdownTables,)

    # Dynamic fields — become template variables
    project_name: str = Field(description="Project name")
```

**Multi-line fields** — use `MultiLineField` for long or multiline content (e.g., review feedback, website content). All multi-line fields are combined into a single XML-tagged user message sent before the main prompt, not inlined in the Context section. Regular `Field()` values must be short, single-line strings (up to 500 chars) — longer or multiline values are auto-promoted to multi-line treatment with a warning:

```python
from ai_pipeline_core import PromptSpec, MultiLineField
from pydantic import Field


class ReviewSpec(PromptSpec):
    """Analyze a review."""

    role = ResearchAnalyst
    input_documents = (SourceDocument,)
    task = "Analyze the review and identify key themes."

    project_name: str = Field(description="Project name")  # Short, inline in prompt
    review: str = MultiLineField(description="Review text")  # Sent as <review>...</review> message
```

**Rendering and sending:**

```python
from ai_pipeline_core import Conversation, render_text, render_preview

# Create spec instance with dynamic field values
spec = AnalysisSpec(project_name="ACME")

# Render prompt text (for inspection/debugging)
prompt = render_text(spec, documents=[source_doc])

# Preview with placeholder values (for debugging)
preview = render_preview(AnalysisSpec)

# Send to LLM via Conversation
conv = await Conversation(model="gemini-3-flash").send_spec(spec, documents=[source_doc])
print(conv.content)  # <result> tags auto-extracted by send_spec()
```

**Structured output** — `output_structure` automatically enables `<result>` tag wrapping, sets a stop sequence at `</result>`, and auto-extracts the content in `Conversation.send_spec()`. `conv.content` returns clean text without tags. Structured output (`PromptSpec[SomeModel]`) uses `send_structured()` automatically.

**Follow-up specs** — use `follows=ParentSpec` to declare follow-up specs. Follow-up specs inherit context from the parent conversation and don't require `role` or `input_documents`.

**CLI tool** for discovery, inspection, rendering, and compilation:

```bash
# Inspect a spec's anatomy (role, docs, fields, rules, output config, token estimate)
ai-prompt-compiler inspect AnalysisSpec

# Render a prompt preview
ai-prompt-compiler render AnalysisSpec

# Discover, list, and compile all specs to .prompts/ directory as markdown files
ai-prompt-compiler compile

# Explicit module:class reference
ai-prompt-compiler render my_package.specs:AnalysisSpec

# Also available as module:
python -m ai_pipeline_core.prompt_compiler inspect AnalysisSpec
```

### Replay

Every LLM conversation call, pipeline task, and pipeline flow is automatically captured as a replayable span in the unified database. When you download a deployment with `ai-trace download`, the snapshot is a portable `FilesystemDatabase`; `ai-replay` can load one recorded span JSON file from that bundle, or replay directly from the database with `--from-db`. Document references are resolved from the database backend by SHA256 at replay time.

**Inspect a recorded span file:**

```bash
ai-replay show ./downloaded_bundle/runs/.../01_conv-a1b2c3d4.json --db-path ./downloaded_bundle
```

**Re-execute with the same parameters:**

```bash
ai-replay run ./downloaded_bundle/runs/.../01_task-build-summary.json --db-path ./downloaded_bundle --import my_app.tasks
```

**Override fields before execution:**

```bash
# Switch model for a recorded conversation span
ai-replay run ./downloaded_bundle/runs/.../01_conv-a1b2c3d4.json --db-path ./downloaded_bundle --import my_app --model grok-4.1-fast

# Override model_options or response_format
ai-replay run ./downloaded_bundle/runs/.../01_conv-a1b2c3d4.json --db-path ./downloaded_bundle --import my_app --set reasoning_effort=low
```

The `--import` flag is required when the original script was run as `__main__` — it imports the module so Document subclasses and functions are registered, and automatically remaps `__main__:X` references to the correct module path.

**Output directory:**

By default, replay writes results to `{replay_file_stem}_replay/` next to the replay file. The output directory is a full `FilesystemDatabase` snapshot containing:

```
conversation_replay/
    output.yaml     # Execution result summary (names, SHA256 references)
    runs/           # Span records
    documents/      # Document metadata
    blobs/          # Raw document content (by SHA256)
```

Override with `--output-dir`:

```bash
ai-replay run ./downloaded_bundle/runs/.../01_task-build-summary.json --db-path ./downloaded_bundle --import my_app --output-dir ./my_output
```

**Database-backed replay:**

Replay resolves document references from a database backend. Use `--db-path` for local snapshots or `--from-db` to replay directly from a recorded span. `show` supports the same database-backed path, and file-backed `show` / `run` reject directories with an actionable error telling you to pass one span JSON file or use `--from-db`.

```bash
ai-replay show --from-db 550e8400-e29b-41d4-a716-446655440000 --db-path ./downloaded_bundle
ai-replay run --from-db 550e8400-e29b-41d4-a716-446655440000 --db-path ./downloaded_bundle
```

**Programmatic replay:**

```python
from pathlib import Path

from ai_pipeline_core.database.filesystem import FilesystemDatabase
from ai_pipeline_core.replay import execute_span

# Open a downloaded bundle read-only
database = FilesystemDatabase(Path("./downloaded_bundle"), read_only=True)

# Replay one recorded span by UUID
result = await execute_span(span_id, source_db=database)
print(result)
```

Replayable span kinds are the same runtime boundaries the framework records: conversation, task, and flow.

### Deployment Downloads

`ai-trace download` exports a deployment as a portable `FilesystemDatabase` snapshot:

```
downloaded_bundle/
  summary.md
  costs.md
  logs.jsonl
  llm_calls.jsonl
  validation.json
  schema_meta.json
  errors.md
  documents.md
  runs/
  documents/
  blobs/
```

`errors.md` is created only when failed nodes exist, and `documents.md` is created only when documents exist. `validation.json` summarizes the staged bundle validation that runs before publish. Under `runs/`, each deployment is stored under a date/name directory with `deployment.json` at the root plus sequence-prefixed child files such as `01_flow-analyze-flow.json`, `01_task-build-summary.json`, and `01_conv-a1b2c3d4.json`. The snapshot is opened read-only by `ai-trace --db-path` and `ai-replay --db-path`.

### `ai-trace` CLI

The `ai-trace` command-line tool provides access to pipeline execution data from the configured database backend or a downloaded `FilesystemDatabase` snapshot.

```bash
# List recent pipeline runs
ai-trace list --limit 10 --status completed

# Show execution summary without downloading
ai-trace show 550e8400-e29b-41d4-a716-446655440000

# Download a deployment as a portable FilesystemDatabase snapshot
ai-trace download 550e8400-e29b-41d4-a716-446655440000 -o ./debug/
```

Connection defaults to `CLICKHOUSE_*` environment variables, or use `--db-path` to point at a local FilesystemDatabase snapshot.

## Configuration

### Environment Variables

```bash
# LLM Configuration (via LiteLLM proxy)
OPENAI_BASE_URL=http://localhost:4000
OPENAI_API_KEY=your-api-key

# Optional: Orchestration
PREFECT_API_URL=http://localhost:4200/api
PREFECT_API_KEY=your-prefect-key
PREFECT_API_AUTH_STRING=your-auth-string
PREFECT_WORK_POOL_NAME=default
PREFECT_WORK_QUEUE_NAME=default
PREFECT_GCS_BUCKET=your-gcs-bucket

# Optional: GCS (for remote storage)
GCS_SERVICE_ACCOUNT_FILE=/path/to/service-account.json

# Optional: Unified Database / Execution Tracking (ClickHouse -- omit for local filesystem store)
CLICKHOUSE_HOST=your-clickhouse-host
CLICKHOUSE_PORT=8443
CLICKHOUSE_DATABASE=default
CLICKHOUSE_USER=default
CLICKHOUSE_PASSWORD=your-password
CLICKHOUSE_SECURE=true
CLICKHOUSE_CONNECT_TIMEOUT=30
CLICKHOUSE_SEND_RECEIVE_TIMEOUT=66

# Optional: Retry Configuration (override class-level None defaults)
TASK_RETRIES=0
TASK_RETRY_DELAY_SECONDS=30
FLOW_RETRIES=0
FLOW_RETRY_DELAY_SECONDS=30
CONVERSATION_RETRIES=2
CONVERSATION_RETRY_DELAY_SECONDS=20

# Optional: Laminar Tracing
LMNR_PROJECT_API_KEY=your-laminar-key

# Optional: Document Summaries (store-level, LLM-generated)
DOC_SUMMARY_ENABLED=true
DOC_SUMMARY_MODEL=gemini-3.1-flash-lite

# Optional: Pub/Sub event delivery (deployment progress/status)
# Requires pubsub_service_type ClassVar on the PipelineDeployment subclass
PUBSUB_PROJECT_ID=your-gcp-project
PUBSUB_TOPIC_ID=pipeline-events
```

### Settings Management

Create custom settings by inheriting from the base Settings class:

```python
from ai_pipeline_core import Settings


class ProjectSettings(Settings):
    """Project-specific configuration."""

    app_name: str = "my-app"
    max_retries: int = 3


# Create singleton instance
settings = ProjectSettings()

# Access configuration (all env vars above are available)
print(settings.openai_base_url)
print(settings.app_name)
```

## Best Practices

### Framework Rules

1. **Pipeline classes**: Subclass `PipelineTask` for tasks and `PipelineFlow` for flows. Use `PipelineDeployment.build_plan()` as the primary deployment authoring path; `build_flows()` remains a fallback wrapper
2. **Task invocation**: Use `await TaskClass.run(...)` for sequential execution and `TaskClass.run(...)` (without await) when you want a parallel `TaskHandle`
3. **Logging**: Use `logging.getLogger(__name__)` -- never `print()`. Logging is auto-configured on import
4. **LLM calls**: Use `Conversation` for all LLM interactions (multi-turn and single-shot). Use `tools=` for function calling
5. **Options**: Omit `ModelOptions` unless specifically needed (defaults are production-optimized)
6. **Documents**: Use `create_root()` for pipeline inputs, `derive()` for content transformations, `create()` for causally triggered documents, `create_external()` for URI provenance. Always subclass `Document`
7. **Type annotations**: Input/output types are in the `run()` method signature. Flows use named `Document` parameters resolved from the deployment blackboard, and tasks use explicit typed inputs with document return annotations
8. **Initialization**: Logger at module scope, not in functions
9. **Document collections**: Use typed tuples like `tuple[MyDocument, ...]` when a task needs a collection input. Flows declare named document parameters, and deployment entrypoints still accept generic document sequences

### Import Convention

Always import from the top-level package when possible:

```python
# Top-level imports (preferred)
from ai_pipeline_core import Document, PipelineTask, PipelineFlow, PipelineDeployment, Conversation, Tool
from ai_pipeline_core import ExternalProvider, StatelessPollingProvider, ProviderOutcome, ProviderError, ProviderAuthError
from ai_pipeline_core import collect_tasks, as_task_completed, TaskHandle, TaskBatch

# Sub-package imports for symbols not at top level
from ai_pipeline_core.llm import ModelOptions
```

## Development

### Dev CLI

All test, lint, and type-check workflows go through the `dev` CLI. It captures full output to `.tmp/dev-runs/` (auto-cleaned after 24h), prints concise summaries with per-step wall-clock durations, auto-detects affected tests from git changes via testmon, warns about slow tests (>30s), and skips reruns when code hasn't changed. Running bare `dev` (no subcommand) shows the info guide.

```bash
# Testing
dev test                # Run affected tests (auto-scoped from git changes, uses testmon)
dev test pipeline       # Run tests for a specific module
dev test --lf           # Rerun only last-failed tests
dev test --full         # Full suite in parallel (before commit)
dev test --available    # Include infrastructure tests (auto-detects Docker/API keys)
dev test --coverage    # Full suite with code coverage (HTML + JSON reports)

# Code quality
dev format              # Auto-fix lint + formatting (ruff format + ruff check --fix)
dev lint                # Check lint without fixing
dev typecheck           # Type checking (basedpyright)

# All checks in order (lint → typecheck → deadcode → semgrep → docstrings → tests)
dev check               # Full validation pipeline
dev check --fast        # Lint + typecheck only (quick sanity check)

# Utilities
dev status              # Show changed files, last run results, suggested next command
dev info                # Detailed usage guide with auto-detected project config
dev list-tests pipeline # List tests by scope
```

**Do not run `pytest`, `ruff`, or `basedpyright` directly** — use `dev` commands instead. A Claude Code hook enforces this in the devcontainer. The `dev` CLI handles correct flags, output management, marker expressions, and idempotency automatically.

**Recommended workflow:**
1. Write code
2. `dev format` — auto-fix lint/formatting
3. `dev check --fast` — verify lint + types
4. `dev test` — run affected tests
5. If tests fail: fix code, then `dev test --lf`
6. `dev check` — full validation before commit

Infrastructure tests (ClickHouse, Pub/Sub, LLM integration) auto-skip when their requirements are unavailable. `dev test` is always safe to run. Use `dev info` to see which infrastructure is detected.

`make` targets (`make test`, `make lint`, `make check`, etc.) delegate to `dev` and remain available as shortcuts.

**Static analysis tools:**
- **Ruff** — 28 rule sets including bugbear, security (bandit), complexity, async enforcement, exception patterns
- **Basedpyright** — strict mode with `reportUnusedCoroutine`, `reportUnreachable`, `reportImplicitStringConcatenation`
- **Vulture** — dead code detection with framework-aware whitelist
- **Semgrep** — custom rules in `.semgrep/` for frozen model mutable fields, async enforcement, docstring quality, architecture constraints
- **Interrogate** — 100% docstring coverage enforcement

### AI Documentation

The `.ai-docs/` directory contains auto-generated API guides designed to be fed directly to AI coding agents as context. Each module produces one self-contained guide — an AI agent should be able to correctly use any module's public API by reading only its guide.

Guides include full source signatures, constraint rules extracted from docstrings, usage examples extracted from tests, and internal types that appear in public API signatures. CI enforces that guides stay fresh with the source code.

```bash
make docs-ai-build  # Generate .ai-docs/ from source code
make docs-ai-check  # Validate .ai-docs/ freshness and completeness
```

When building applications on this framework, include the relevant `.ai-docs/*.md` guides in your AI agent's context window.

## Examples

The `examples/` directory contains:

- **`showcase.py`** -- Full 3-stage pipeline: class-based PipelineTask/PipelineFlow, Conversation API, multi-turn LLM analysis, structured extraction, PipelineDeployment with CLI, resume/skip, progress tracking, image processing
- **`showcase_database.py`** -- Database usage: run a real deployment into `MemoryDatabase`, inspect a recorded task span target plus parsed `meta_json` / `metrics_json`, and inspect output document ancestry
- **`showcase_replay.py`** -- Replay system: record a real task span through `PipelineDeployment.run(...)`, then replay that stored span with `execute_span()`
- **`showcase_prompt_compiler.py`** -- Prompt compiler features: Role, Rule, OutputRule, Guide, PromptSpec, rendering, `Conversation.send_spec()` usage patterns, follow-up specs, definition-time validation
- **`showcase_stubs.py`** -- Stub classes for incremental development: `stub=True` on PipelineTask, PipelineFlow, PromptSpec with preserved type contracts, runtime guards, and deployment blocking

Run examples:
```bash
# Full pipeline showcase (requires OPENAI_BASE_URL and OPENAI_API_KEY)
python examples/showcase.py ./output

# Database showcase (no arguments needed)
python examples/showcase_database.py

# Replay showcase (no arguments needed)
python examples/showcase_replay.py

# Prompt compiler showcase (no arguments needed)
python examples/showcase_prompt_compiler.py
```

## Project Structure

```
ai-pipeline-core/
|-- ai_pipeline_core/
|   |-- _llm_core/        # Internal LLM client, model types, and response handling
|   |-- deployment/        # Pipeline deployment, deploy script, CLI bootstrap, progress, remote
|   |-- database/          # Execution DAG, documents, blobs, logs, and download helpers
|   |-- documents/         # Document system (Document base class, attachments, context)
|   |-- llm/               # Conversation class, Tool base class, tool loop, URLSubstitutor, image processing
|   |-- logger/            # Logging infrastructure
|   |-- observability/     # Database-backed execution CLI (`ai-trace`)
|   |-- pipeline/          # PipelineTask, PipelineFlow, parallel primitives, FlowOptions, concurrency limits
|   |-- prompt_compiler/   # Type-safe prompt specs, rendering, and CLI tool
|   |-- replay/            # Replay system (capture, serialize, resolve, execute)
|   |-- providers.py       # External provider base classes (ExternalProvider, StatelessPollingProvider)
|   |-- settings.py        # Configuration management (Pydantic BaseSettings)
|   +-- exceptions.py      # Framework exceptions (LLMError, DocumentNameError, etc.)
|-- tools/
|   |-- dev-cli/           # Dev CLI — enforces correct test/lint/check workflows
|   |-- docs-generator/    # AI-focused documentation generator (separate workspace package)
|   +-- trace-inspector/   # Trace inspection — generates markdown debug bundles from execution data
|-- .ai-docs/             # Auto-generated API guides for AI coding agents
|-- tests/                 # Comprehensive test suite
|-- examples/              # Usage examples
+-- pyproject.toml         # Project configuration
```

## Contributing

1. Fork the repository
2. Create a feature branch
3. Make changes — run `dev check` (must pass all linting, type checking, semgrep, and tests)
4. Open a Pull Request

Note: This is an internal-first framework. External contributions are welcome but the architecture and infrastructure choices (Prefect, ClickHouse) are fixed.

## License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.

## Support

- **Issues**: [GitHub Issues](https://github.com/researchtech-inc/ai-pipeline-core/issues)
- **Discussions**: [GitHub Discussions](https://github.com/researchtech-inc/ai-pipeline-core/discussions)

## Acknowledgments

- Built on [Prefect](https://www.prefect.io/) for workflow orchestration
- Uses [LiteLLM](https://github.com/BerriAI/litellm) for LLM provider abstraction (also compatible with [OpenRouter](https://openrouter.ai/))
- Type checking with [Pydantic](https://pydantic.dev/) and [basedpyright](https://github.com/DetachHead/basedpyright)
