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

from pydantic import Field

from . import _llm_core_bootstrap as _llm_core_bootstrap  # populate _llm_core config from Settings
from . import llm
from ._codec import CodecError, UniversalCodec
from ._pydantic_base import FrozenBaseModel
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
    ContentPolicyError,
    DocumentNameError,
    DocumentSizeError,
    DocumentValidationError,
    GroupExhaustedError,
    LLMError,
    LLMValidationError,
    MidStreamProviderError,
    NonRetriableError,
    PartialToolCallStreamError,
    PayloadTooLargeError,
    PipelineCoreError,
    ProseContaminationError,
    RetryableError,
    StreamWatchdogError,
    StrictModeViolationError,
    StructuredSchemaError,
    StubNotImplementedError,
    TerminalError,
)
from .llm import (
    AIModel,
    AIModelRef,
    Citation,
    Conversation,
    ImagePreset,
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
from .prompt_compiler import (
    Guide,
    ListField,
    MultiLineField,
    OutputRule,
    OutputT,
    PromptSpec,
    Role,
    Rule,
    StructuredField,
    render_preview,
    render_text,
)
from .prompt_contract import (
    CitedText,
    Continues,
    DocumentCitation,
    Methodology,
    PromptContract,
    PromptResult,
    ToolAvailability,
    ToolBinding,
    ValidationFailure,
)
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
    "AIModel",
    "AIModelRef",
    "Attachment",
    "Citation",
    "CitedText",
    "CodecError",
    "ContentPolicyError",
    "Continues",
    "Conversation",
    "DeploymentPlan",
    "DeploymentResult",
    "Document",
    "DocumentCitation",
    "DocumentNameError",
    "DocumentSha256",
    "DocumentSizeError",
    "DocumentValidationError",
    "ExperimentOverrides",
    "ExperimentResult",
    "ExternalProvider",
    "Field",
    "FieldGate",
    "FlowOptions",
    "FlowOutputs",
    "FlowStep",
    "FrozenBaseModel",
    "GroupExhaustedError",
    "Guide",
    "ImagePreset",
    "LLMError",
    "LLMValidationError",
    "LimitKind",
    "ListField",
    "Methodology",
    "MidStreamProviderError",
    "ModelOptions",
    "MultiLineField",
    "NonRetriableError",
    "OutputRule",
    "OutputT",
    "PartialToolCallStreamError",
    "PayloadTooLargeError",
    "PipelineCoreError",
    "PipelineDeployment",
    "PipelineFlow",
    "PipelineLimit",
    "PipelineTask",
    "PromptContract",
    "PromptResult",
    "PromptSpec",
    "ProseContaminationError",
    "ProviderAuthError",
    "ProviderError",
    "ProviderOutcome",
    "RemoteDeployment",
    "RetryableError",
    "Role",
    "Rule",
    "Settings",
    "StatelessPollingProvider",
    "StreamWatchdogError",
    "StrictModeViolationError",
    "StructuredField",
    "StructuredSchemaError",
    "StubNotImplementedError",
    "TaskBatch",
    "TaskHandle",
    "TerminalError",
    "TokenUsage",
    "Tool",
    "ToolAvailability",
    "ToolBinding",
    "ToolOutput",
    "UniversalCodec",
    "ValidationFailure",
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
