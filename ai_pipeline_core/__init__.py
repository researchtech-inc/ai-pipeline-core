"""AI Pipeline Core - Production-ready framework for building AI pipelines with LLMs."""

import importlib.metadata
import os
import sys

# Disable Prefect telemetry and analytics before importing Prefect.
# Execution tracking is handled by the framework database.
os.environ.setdefault("DO_NOT_TRACK", "1")
os.environ.setdefault("PREFECT_CLOUD_ENABLE_ORCHESTRATION_TELEMETRY", "false")

from prefect.context import refresh_global_settings_context
from prefect.settings import get_current_settings

# If Prefect was already imported (user imported it before us), refresh its cached settings.
if "prefect" in sys.modules and get_current_settings().cloud.enable_orchestration_telemetry:
    refresh_global_settings_context()

from . import llm
from ._codec import CodecError, UniversalCodec
from .deployment import DeploymentPlan, DeploymentResult, FieldGate, FlowOutputs, FlowStep, PipelineDeployment
from .deployment.remote import RemoteDeployment
from .documents import (
    Attachment,
    Document,
    DocumentSha256,
    ensure_extension,
    find_document,
    replace_extension,
    sanitize_url,
)
from .exceptions import (
    DocumentNameError,
    DocumentSizeError,
    DocumentValidationError,
    LLMError,
    NonRetriableError,
    OutputDegenerationError,
    PipelineCoreError,
    StubNotImplementedError,
)
from .llm import (
    Citation,
    Conversation,
    ModelName,
    ModelOptions,
    TokenUsage,
    Tool,
    ToolOutput,
)
from .pipeline import (
    FlowOptions,
    LimitKind,
    PipelineFlow,
    PipelineLimit,
    PipelineTask,
    TaskBatch,
    TaskHandle,
    add_cost,
    as_task_completed,
    collect_tasks,
    get_run_id,
    pipeline_concurrency,
    pipeline_test_context,
    safe_gather,
    safe_gather_indexed,
    traced_operation,
)
from .prompt_compiler import Guide, ListField, MultiLineField, OutputRule, OutputT, PromptSpec, Role, Rule, StructuredField, render_preview, render_text
from .providers import (
    ExternalProvider,
    ProviderAuthError,
    ProviderError,
    ProviderOutcome,
    StatelessPollingProvider,
)
from .replay import ExperimentOverrides, ExperimentResult, execute_span, experiment_batch, experiment_span
from .settings import Settings

__version__ = importlib.metadata.version("ai-pipeline-core")

__all__ = [
    "Attachment",
    "Citation",
    "CodecError",
    "Conversation",
    "DeploymentPlan",
    "DeploymentResult",
    "Document",
    "DocumentNameError",
    "DocumentSha256",
    "DocumentSizeError",
    "DocumentValidationError",
    "ExperimentOverrides",
    "ExperimentResult",
    "ExternalProvider",
    "FieldGate",
    "FlowOptions",
    "FlowOutputs",
    "FlowStep",
    "Guide",
    "LLMError",
    "LimitKind",
    "ListField",
    "ModelName",
    "ModelOptions",
    "MultiLineField",
    "NonRetriableError",
    "OutputDegenerationError",
    "OutputRule",
    "OutputT",
    "PipelineCoreError",
    "PipelineDeployment",
    "PipelineFlow",
    "PipelineLimit",
    "PipelineTask",
    "PromptSpec",
    "ProviderAuthError",
    "ProviderError",
    "ProviderOutcome",
    "RemoteDeployment",
    "Role",
    "Rule",
    "Settings",
    "StatelessPollingProvider",
    "StructuredField",
    "StubNotImplementedError",
    "TaskBatch",
    "TaskHandle",
    "TokenUsage",
    "Tool",
    "ToolOutput",
    "UniversalCodec",
    "add_cost",
    "as_task_completed",
    "collect_tasks",
    "ensure_extension",
    "execute_span",
    "experiment_batch",
    "experiment_span",
    "find_document",
    "get_run_id",
    "llm",
    "pipeline_concurrency",
    "pipeline_test_context",
    "render_preview",
    "render_text",
    "replace_extension",
    "safe_gather",
    "safe_gather_indexed",
    "sanitize_url",
    "traced_operation",
]
