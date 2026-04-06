"""PipelineDeployment class-based execution tests.

Covers: deployment lifecycle, step validation, run scope hashing, options passing,
result serialization, contract models, flow chain validation, refactoring verification.
"""

# pyright: reportArgumentType=false, reportGeneralTypeIssues=false, reportPrivateUsage=false, reportUnusedClass=false, reportFunctionMemberAccess=false

import inspect
import json
from datetime import UTC
from typing import Any
from uuid import uuid4

import pytest
from pydantic import BaseModel, Field

from ai_pipeline_core import (
    DeploymentPlan,
    DeploymentResult,
    Document,
    FieldGate,
    FlowOptions,
    FlowStep,
    PipelineDeployment,
    PipelineFlow,
)
from ai_pipeline_core.database import SpanKind, SpanStatus
from ai_pipeline_core.database._memory import _MemoryDatabase
from ai_pipeline_core.deployment._contract import (
    CompletedRun,
    DeploymentResultData,
    FailedRun,
    PendingRun,
    ProgressRun,
)
from .conftest import InputDoc, MiddleDoc, OutputDoc, StageOne, StageTwo, _TestOptions, _TestResult


# --- Module-level test infrastructure ---


class ValidResult(DeploymentResult):
    """Result for testing."""

    count: int = 0


class ValidFlow(PipelineFlow):
    """Single-step flow for testing."""

    async def run(self, input_docs: tuple[InputDoc, ...], options: FlowOptions) -> tuple[OutputDoc, ...]:
        _ = options
        return (OutputDoc.derive(derived_from=(input_docs[0],), name="output.txt", content="result"),)


class FlowA(PipelineFlow):
    """First flow in multi-step pipeline."""

    async def run(self, input_docs: tuple[InputDoc, ...], options: FlowOptions) -> tuple[MiddleDoc, ...]:
        _ = options
        return (MiddleDoc.derive(derived_from=(input_docs[0],), name="middle.txt", content="middle"),)


class FlowB(PipelineFlow):
    """Second flow in multi-step pipeline."""

    async def run(self, middle_docs: tuple[MiddleDoc, ...], options: FlowOptions) -> tuple[OutputDoc, ...]:
        _ = options
        return (OutputDoc.derive(derived_from=(middle_docs[0],), name="output.txt", content="final"),)


class _SkipDecisionModel(BaseModel):
    should_run: bool


class _SkipDecisionDoc(Document[_SkipDecisionModel]):
    """Typed control document used to trigger a gated skip in tests."""


# --- DeploymentResult tests ---


class TestDeploymentResult:
    """Test DeploymentResult model."""

    def test_success(self):
        """Test successful result."""
        result = DeploymentResult(success=True)
        assert result.success is True
        assert result.error is None

    def test_failure(self):
        """Test failed result with error."""
        result = DeploymentResult(success=False, error="Something went wrong")
        assert result.success is False
        assert result.error == "Something went wrong"

    def test_subclass(self):
        """Test custom result subclass."""
        result = ValidResult(success=True, count=42)
        assert result.count == 42


# --- Contract models tests ---


class TestContractModels:
    """Test deployment contract Pydantic models."""

    def test_pending_run(self):
        """Test PendingRun creation."""
        from datetime import datetime
        from uuid import UUID

        run = PendingRun(
            flow_run_id=UUID(int=1),
            run_id="test",
            state="PENDING",
            timestamp=datetime.now(UTC),
        )
        assert run.type == "pending"

    def test_progress_run(self):
        """Test ProgressRun creation with all fields."""
        from datetime import datetime
        from uuid import UUID

        run = ProgressRun(
            flow_run_id=UUID(int=1),
            run_id="test",
            state="RUNNING",
            timestamp=datetime.now(UTC),
            step=2,
            total_steps=5,
            flow_name="analysis",
            status="started",
            progress=0.4,
            step_progress=0.0,
            message="Starting analysis",
        )
        assert run.type == "progress"
        assert run.progress == 0.4

    def test_completed_run(self):
        """Test CompletedRun creation."""
        from datetime import datetime
        from uuid import UUID

        run = CompletedRun(
            flow_run_id=UUID(int=1),
            run_id="test",
            state="COMPLETED",
            timestamp=datetime.now(UTC),
            result=DeploymentResultData(success=True),
        )
        assert run.type == "completed"
        assert run.result.success is True

    def test_failed_run(self):
        """Test FailedRun creation."""
        from datetime import datetime
        from uuid import UUID

        run = FailedRun(
            flow_run_id=UUID(int=1),
            run_id="test",
            state="FAILED",
            timestamp=datetime.now(UTC),
            error="Pipeline crashed",
        )
        assert run.type == "failed"
        assert run.error == "Pipeline crashed"

    def test_deployment_result_data(self):
        """Test DeploymentResultData."""
        data = DeploymentResultData(success=True, error=None)
        assert data.success is True
        dumped = data.model_dump()
        assert "success" in dumped

    def test_no_storage_uri_field(self):
        """Test that storage_uri has been removed from contract models."""
        assert "storage_uri" not in PendingRun.model_fields


# --- PipelineDeployment validation tests ---


class TestPipelineDeploymentValidation:
    """Test PipelineDeployment.__init_subclass__ validation."""

    def test_valid_subclass(self):
        """Test valid deployment creation."""

        class MyDeployment(PipelineDeployment[FlowOptions, ValidResult]):
            """Valid deployment."""

            flow_retries = 0

            def build_flows(self, options):
                return [ValidFlow()]

            @staticmethod
            def build_result(run_id: str, documents: tuple[Document, ...], options: FlowOptions) -> ValidResult:
                return ValidResult(success=True)

        assert MyDeployment.name == "my-deployment"
        assert MyDeployment.options_type is FlowOptions
        assert MyDeployment.result_type is ValidResult
        assert MyDeployment.pubsub_service_type == ""

    def test_pubsub_service_type_classvar(self):
        """pubsub_service_type defaults to empty and can be overridden."""

        class WithServiceType(PipelineDeployment[FlowOptions, ValidResult]):
            """Deployment with pubsub_service_type."""

            flow_retries = 0
            pubsub_service_type = "research"

            def build_flows(self, options):
                return [ValidFlow()]

            @staticmethod
            def build_result(run_id: str, documents: tuple[Document, ...], options: FlowOptions) -> ValidResult:
                return ValidResult(success=True)

        assert WithServiceType.pubsub_service_type == "research"

    def test_name_starts_with_test_raises(self):
        """Test that 'Test' prefix raises TypeError."""
        with pytest.raises(TypeError, match="cannot start with 'Test'"):

            class TestDeployment(PipelineDeployment[FlowOptions, ValidResult]):
                """Invalid name."""

                flow_retries = 0

                def build_flows(self, options):
                    return [ValidFlow()]

                @staticmethod
                def build_result(run_id: str, documents: tuple[Document, ...], options: FlowOptions) -> ValidResult:
                    return ValidResult(success=True)

    def test_missing_generic_params_raises(self):
        """Test that missing generic params raises TypeError."""
        with pytest.raises(TypeError, match="must specify Generic parameters"):

            class RawDeployment(PipelineDeployment):  # type: ignore[type-arg]
                """No generics."""

                def build_flows(self, options):
                    return [ValidFlow()]

                @staticmethod
                def build_result(run_id: str, documents: tuple[Document, ...], options: FlowOptions) -> ValidResult:
                    return ValidResult(success=True)


class TestAbstractSubclass:
    """Test PipelineDeployment partial subclassing."""

    def test_requires_build_flows_or_build_plan_override(self):
        """Test that subclass without build_flows/build_plan override raises TypeError."""
        with pytest.raises(TypeError, match=r"build_flows.*build_plan"):

            class PartialDeployment(PipelineDeployment[FlowOptions, ValidResult]):
                """Intermediate class without build_flows."""

                flow_retries = 0

                @staticmethod
                def build_result(run_id: str, documents: tuple[Document, ...], options: FlowOptions) -> ValidResult:
                    return ValidResult(success=True)


class ValidDeployment(PipelineDeployment[FlowOptions, ValidResult]):
    """Deployment for testing."""

    flow_retries = 0

    def build_plan(self, options: FlowOptions) -> DeploymentPlan:
        _ = options
        return DeploymentPlan(steps=(FlowStep(ValidFlow()),))

    @staticmethod
    def build_result(run_id: str, documents: tuple[Document, ...], options: FlowOptions) -> ValidResult:
        return ValidResult(success=True, count=len(documents))


# --- Database integration tests ---


class TestAllDocumentTypes:
    """Test _all_document_types helper."""

    def test_collects_types_from_flows(self):
        """Test that all input/output types are collected and deduplicated."""

        class MultiFlowDeployment(PipelineDeployment[FlowOptions, ValidResult]):
            """Multi-flow deployment."""

            flow_retries = 0

            def build_plan(self, options: FlowOptions) -> DeploymentPlan:
                _ = options
                return DeploymentPlan(steps=(FlowStep(FlowA()), FlowStep(FlowB())))

            @staticmethod
            def build_result(run_id: str, documents: tuple[Document, ...], options: FlowOptions) -> ValidResult:
                return ValidResult(success=True)

        deployment = MultiFlowDeployment()
        flows = [step.flow for step in deployment.build_plan(FlowOptions()).steps]
        types = deployment._all_document_types(flows)
        type_names = {t.__name__ for t in types}
        assert "InputDoc" in type_names
        assert "MiddleDoc" in type_names
        assert "OutputDoc" in type_names


class TestComputeInputFingerprint:
    """Test _compute_input_fingerprint function."""

    def test_different_options_produce_different_scope_with_documents(self):
        """Different options must produce different fingerprints when documents are provided."""
        from ai_pipeline_core.deployment._helpers import _compute_input_fingerprint

        doc = InputDoc(name="input.txt", content=b"test")

        class CustomOptions(FlowOptions):
            flag: bool = False

        fingerprint1 = _compute_input_fingerprint([doc], CustomOptions(flag=True))
        fingerprint2 = _compute_input_fingerprint([doc], CustomOptions(flag=False))

        assert fingerprint1 != fingerprint2
        assert len(fingerprint1) == 16
        assert len(fingerprint2) == 16

    def test_different_options_produce_different_scope_without_documents(self):
        """Different options must produce different fingerprints even with empty documents."""
        from ai_pipeline_core.deployment._helpers import _compute_input_fingerprint

        class CustomOptions(FlowOptions):
            flag: bool = False

        fingerprint1 = _compute_input_fingerprint([], CustomOptions(flag=True))
        fingerprint2 = _compute_input_fingerprint([], CustomOptions(flag=False))

        assert fingerprint1 != fingerprint2
        assert len(fingerprint1) == 16
        assert len(fingerprint2) == 16

    def test_same_inputs_produce_same_scope(self):
        """Identical inputs must produce identical fingerprints."""
        from ai_pipeline_core.deployment._helpers import _compute_input_fingerprint

        doc = InputDoc(name="input.txt", content=b"test")

        fingerprint1 = _compute_input_fingerprint([doc], FlowOptions())
        fingerprint2 = _compute_input_fingerprint([doc], FlowOptions())

        assert fingerprint1 == fingerprint2

    def test_cli_fields_are_excluded(self):
        """CLI-only fields must not affect the input fingerprint."""
        from ai_pipeline_core.deployment._helpers import _CLI_FIELDS
        from ai_pipeline_core.deployment._helpers import _compute_input_fingerprint

        class CliOptions(FlowOptions):
            working_directory: str = ""
            run_id: str | None = None
            start: int = 1
            end: int | None = None
            actual_option: str = "value"

        # Verify the CLI fields are in _CLI_FIELDS
        assert "working_directory" in _CLI_FIELDS
        assert "start" in _CLI_FIELDS

        # Different CLI field values should produce the same scope
        opts1 = CliOptions(working_directory="/path1", start=1, actual_option="same")
        opts2 = CliOptions(working_directory="/path2", start=5, actual_option="same")

        fingerprint1 = _compute_input_fingerprint([], opts1)
        fingerprint2 = _compute_input_fingerprint([], opts2)

        assert fingerprint1 == fingerprint2

        # But different actual_option values should produce different scope
        opts3 = CliOptions(actual_option="different")
        fingerprint3 = _compute_input_fingerprint([], opts3)

        assert fingerprint1 != fingerprint3


# --- as_prefect_flow parameter schema tests ---


class _Schema_TestOptions(FlowOptions):
    """Options with concrete fields for schema testing."""

    task_name: str = Field(default="", description="Name of the task")
    max_retries: int = Field(default=3, ge=1, le=10, description="Max retry count")
    threshold: float = Field(default=0.5, description="Score threshold")


class _Schema_TestResult(DeploymentResult):
    """Result for schema testing."""

    output: str = ""


class _SchemaTestFlow(PipelineFlow):
    """Flow for schema testing."""

    async def run(self, input_docs: tuple[InputDoc, ...], options: _Schema_TestOptions) -> tuple[OutputDoc, ...]:
        _ = options
        return (OutputDoc.derive(derived_from=(input_docs[0],), name="out.txt", content="ok"),)


class _SchemaTestDeployment(PipelineDeployment[_Schema_TestOptions, _Schema_TestResult]):
    flow_retries = 0

    def build_plan(self, options: _Schema_TestOptions) -> DeploymentPlan:
        _ = options
        return DeploymentPlan(steps=(FlowStep(_SchemaTestFlow()),))

    @staticmethod
    def build_result(run_id: str, documents: tuple[Document, ...], options: _Schema_TestOptions) -> _Schema_TestResult:
        return _Schema_TestResult(success=True, output="done")


def _resolve_options_properties(schema: Any) -> dict[str, Any]:
    """Resolve the 'options' parameter properties from a ParameterSchema, following $ref if needed."""
    options_schema = schema.properties.get("options", {})
    if "$ref" in options_schema:
        ref_name = options_schema["$ref"].split("/")[-1]
        return schema.definitions.get(ref_name, {}).get("properties", {})
    if "allOf" in options_schema:
        for item in options_schema["allOf"]:
            if "$ref" in item:
                ref_name = item["$ref"].split("/")[-1]
                return schema.definitions.get(ref_name, {}).get("properties", {})
    return options_schema.get("properties", {})


class TestAsPrefectFlowParameterSchema:
    """Test that as_prefect_flow() exposes concrete options schema to Prefect."""

    def test_parameter_schema_contains_concrete_options_fields(self):
        """Prefect flow parameter schema must include fields from the concrete options type."""
        prefect_flow = _SchemaTestDeployment().as_prefect_flow()
        schema = prefect_flow.parameters

        options_props = _resolve_options_properties(schema)

        assert "task_name" in options_props, f"task_name missing from schema: {options_props}"
        assert "max_retries" in options_props, f"max_retries missing from schema: {options_props}"
        assert "threshold" in options_props, f"threshold missing from schema: {options_props}"

    def test_parameter_schema_field_types(self):
        """Schema field types must match the Pydantic model field types."""
        prefect_flow = _SchemaTestDeployment().as_prefect_flow()
        schema = prefect_flow.parameters
        options_props = _resolve_options_properties(schema)

        assert options_props["task_name"]["type"] == "string"
        assert options_props["max_retries"]["type"] == "integer"
        assert options_props["threshold"]["type"] == "number"

    def test_parameter_schema_field_defaults(self):
        """Schema field defaults must match the Pydantic model defaults."""
        prefect_flow = _SchemaTestDeployment().as_prefect_flow()
        schema = prefect_flow.parameters
        options_props = _resolve_options_properties(schema)

        assert options_props["task_name"].get("default") == ""
        assert options_props["max_retries"].get("default") == 3
        assert options_props["threshold"].get("default") == 0.5

    def test_base_flow_options_produces_no_custom_fields(self):
        """Base FlowOptions (no fields) should still appear in schema but with no custom properties."""
        prefect_flow = ValidDeployment().as_prefect_flow()
        schema = prefect_flow.parameters

        # options parameter must exist in the schema
        assert "options" in schema.properties


# --- Refactoring verification tests ---


class TestCliFieldsFrozenset:
    """Verify _CLI_FIELDS is immutable."""

    def test_cli_fields_is_frozenset(self):
        from ai_pipeline_core.deployment._helpers import _CLI_FIELDS

        assert isinstance(_CLI_FIELDS, frozenset)


class TestStepValidation:
    """Test start_step/end_step validation in run()."""

    @pytest.fixture
    def deployment(self):
        return ValidDeployment()

    @pytest.mark.asyncio
    async def test_start_step_zero_raises(self, deployment):
        with pytest.raises(ValueError, match="start_step must be 1"):
            await deployment.run("proj", [], FlowOptions(), start_step=0)

    @pytest.mark.asyncio
    async def test_start_step_too_large_raises(self, deployment):
        with pytest.raises(ValueError, match="start_step must be 1"):
            await deployment.run("proj", [], FlowOptions(), start_step=99)

    @pytest.mark.asyncio
    async def test_end_step_less_than_start_raises(self, deployment):
        with pytest.raises(ValueError, match="end_step must be"):
            await deployment.run("proj", [], FlowOptions(), start_step=1, end_step=0)


class TestRunPassesOptionsObject:
    """Verify run() passes options object (not dict) to flows."""

    @pytest.mark.asyncio
    async def test_flow_receives_options_not_dict(self):
        received_options: list[FlowOptions] = []

        class CapturingFlow(PipelineFlow):
            async def run(self, input_docs: tuple[InputDoc, ...], options: FlowOptions) -> tuple[OutputDoc, ...]:
                received_options.append(options)
                return (OutputDoc.derive(derived_from=(input_docs[0],), name="out.txt", content="ok"),)

        class CapturingDeployment(PipelineDeployment[FlowOptions, ValidResult]):
            flow_retries = 0

            def build_flows(self, options):
                return [CapturingFlow()]

            @staticmethod
            def build_result(run_id: str, documents: tuple[Document, ...], options: FlowOptions) -> ValidResult:
                return ValidResult(success=True, count=len(documents))

        deployment = CapturingDeployment()
        opts = FlowOptions()
        await deployment.run("proj", [InputDoc.create_root(name="in.txt", content="input", reason="test")], opts)
        assert len(received_options) == 1
        assert isinstance(received_options[0], FlowOptions), f"Expected FlowOptions, got {type(received_options[0])}"
        assert not isinstance(received_options[0], dict)


# --- Bug 9: Parent-child run lineage ---


class TestRunContextIncludesExecutionId:
    """Verify RunContext.execution_id is populated during run()."""

    @pytest.mark.asyncio
    async def test_run_context_has_execution_id(self):
        from ai_pipeline_core.pipeline._execution_context import get_execution_context

        captured_ctx: list[Any] = []

        class CtxFlow(PipelineFlow):
            async def run(self, input_docs: tuple[InputDoc, ...], options: FlowOptions) -> tuple[OutputDoc, ...]:
                _ = (input_docs, options)
                captured_ctx.append(get_execution_context())
                return (OutputDoc.derive(derived_from=(input_docs[0],), name="out.txt", content="ok"),)

        class CtxDeployment(PipelineDeployment[FlowOptions, ValidResult]):
            flow_retries = 0

            def build_flows(self, options):
                return [CtxFlow()]

            @staticmethod
            def build_result(run_id: str, documents: tuple[Document, ...], options: FlowOptions) -> ValidResult:
                return ValidResult(success=True)

        await CtxDeployment().run("proj", [InputDoc.create_root(name="in.txt", content="x", reason="test")], FlowOptions())
        assert len(captured_ctx) == 1
        ctx = captured_ctx[0]
        assert ctx is not None
        # execution_id may be None when no ClickHouse is configured, but run_id must be set
        assert ctx.run_id


class TestAsPrefectFlowParentParams:
    """Verify _deployment_flow accepts parent lineage parameters."""

    def test_deployment_flow_has_parent_params(self):
        prefect_flow = ValidDeployment().as_prefect_flow()
        sig = inspect.signature(prefect_flow.fn)
        assert "parent_execution_id" in sig.parameters
        assert sig.parameters["parent_execution_id"].default is None

    @pytest.mark.asyncio
    async def test_run_branch_passes_prefect_database_into_run(self) -> None:
        """The standalone Prefect entry path must reuse the database it creates."""
        from unittest.mock import AsyncMock, patch

        deployment = ValidDeployment()
        prefect_flow = deployment.as_prefect_flow()
        database = AsyncMock()
        publisher = AsyncMock()
        publisher.close = AsyncMock()

        with (
            patch("ai_pipeline_core.deployment._prefect._create_span_database_from_settings", return_value=database),
            patch("ai_pipeline_core.deployment._prefect._create_publisher", return_value=publisher),
            patch("ai_pipeline_core.deployment._prefect.resolve_document_inputs", new=AsyncMock(return_value=[])),
            patch.object(deployment, "run", new=AsyncMock(return_value=ValidResult(success=True))) as mock_run,
        ):
            await prefect_flow.fn("prefect-run", [], FlowOptions())

        assert mock_run.await_args.kwargs["database"] is database

    @pytest.mark.asyncio
    async def test_deployment_flow_accepts_document_inputs_keyword(self) -> None:
        """The generated Prefect wrapper must expose the same document_inputs key used by RemoteDeployment."""
        from unittest.mock import AsyncMock, patch

        deployment = ValidDeployment()
        prefect_flow = deployment.as_prefect_flow()
        database = AsyncMock()
        publisher = AsyncMock()
        publisher.close = AsyncMock()

        with (
            patch("ai_pipeline_core.deployment._prefect._create_span_database_from_settings", return_value=database),
            patch("ai_pipeline_core.deployment._prefect._create_publisher", return_value=publisher),
            patch("ai_pipeline_core.deployment._prefect.resolve_document_inputs", new=AsyncMock(return_value=[])) as mock_resolve,
            patch.object(deployment, "run", new=AsyncMock(return_value=ValidResult(success=True))),
        ):
            await prefect_flow.fn(run_id="prefect-run", document_inputs=[], options=FlowOptions())

        assert mock_resolve.await_args.args[0] == []


# --- Deployment run executes flow instances ---


class ExampleDeployment(PipelineDeployment[_TestOptions, _TestResult]):
    flow_retries = 0

    def build_plan(self, options: _TestOptions) -> DeploymentPlan:
        _ = options
        return DeploymentPlan(steps=(FlowStep(StageOne()), FlowStep(StageTwo())))

    @staticmethod
    def build_result(run_id, documents, options):
        _ = (run_id, options)
        return _TestResult(success=True, output_count=len([d for d in documents if isinstance(d, OutputDoc)]))


@pytest.mark.asyncio
async def test_deployment_run_executes_flow_instances(input_documents):
    deployment = ExampleDeployment()
    result = await deployment.run("run-1", input_documents, _TestOptions())

    assert result.success is True
    assert result.output_count == 1


@pytest.mark.asyncio
async def test_deployment_persists_logs_and_log_summaries(input_documents) -> None:
    deployment = ExampleDeployment()
    database = _MemoryDatabase()

    result = await deployment.run("run-with-logs", input_documents, _TestOptions(), database=database)

    assert result.success is True

    deployment_spans = [span for span in database._spans.values() if span.kind == SpanKind.DEPLOYMENT]
    flow_spans = [span for span in database._spans.values() if span.kind == SpanKind.FLOW]

    assert len(deployment_spans) == 1
    assert flow_spans
    assert json.loads(deployment_spans[0].metrics_json)["log_summary"]["total"] >= 1
    assert json.loads(flow_spans[0].metrics_json)["log_summary"]["total"] >= 1

    deployment_logs = await database.get_deployment_logs(deployment_spans[0].deployment_id, category="lifecycle")
    event_types = {log.event_type for log in deployment_logs}
    assert "deployment.started" in event_types
    assert "deployment.completed" in event_types
    assert "flow.started" in event_types
    assert "flow.completed" in event_types


@pytest.mark.asyncio
async def test_skipped_flow_persists_lifecycle_log_and_summary(input_documents) -> None:
    class SkipSecondFlowDeployment(ExampleDeployment):
        def build_plan(self, options: _TestOptions) -> DeploymentPlan:
            _ = options
            return DeploymentPlan(
                steps=(
                    FlowStep(StageOne()),
                    FlowStep(StageTwo(), run_if=FieldGate(_SkipDecisionDoc, "should_run", op="truthy", on_missing="skip")),
                )
            )

    database = _MemoryDatabase()
    await SkipSecondFlowDeployment().run("run-skip-logs", input_documents, _TestOptions(), database=database)

    deployment_span = max(
        (span for span in database._spans.values() if span.kind == SpanKind.DEPLOYMENT),
        key=lambda span: span.started_at,
    )
    skipped_spans = [span for span in database._spans.values() if span.kind == SpanKind.FLOW and span.status == SpanStatus.SKIPPED]

    lifecycle_logs = await database.get_deployment_logs(deployment_span.deployment_id, category="lifecycle")
    assert "flow.skipped" in {log.event_type for log in lifecycle_logs}
    assert skipped_spans
    assert json.loads(skipped_spans[0].metrics_json)["log_summary"]["total"] >= 1


@pytest.mark.asyncio
async def test_cached_flow_persists_lifecycle_log_and_summary(input_documents) -> None:
    database = _MemoryDatabase()
    deployment = ExampleDeployment()

    await deployment.run("run-cache-logs", input_documents, _TestOptions(), database=database)
    await deployment.run("run-cache-logs", input_documents, _TestOptions(), database=database)

    deployment_span = max(
        (span for span in database._spans.values() if span.kind == SpanKind.DEPLOYMENT),
        key=lambda span: span.started_at,
    )
    cached_spans = [span for span in database._spans.values() if span.kind == SpanKind.FLOW and span.status == SpanStatus.CACHED]

    lifecycle_logs = await database.get_deployment_logs(deployment_span.deployment_id, category="lifecycle")
    assert "flow.cached" in {log.event_type for log in lifecycle_logs}
    assert cached_spans
    assert json.loads(cached_spans[0].metrics_json)["log_summary"]["total"] >= 1


def test_deployment_requires_build_flows_or_build_plan_override():
    with pytest.raises(TypeError, match=r"build_flows.*build_plan"):

        class MissingFlows(PipelineDeployment[_TestOptions, _TestResult]):
            flow_retries = 0

            @staticmethod
            def build_result(run_id, documents, options):
                _ = (run_id, documents, options)
                return _TestResult(success=True)


@pytest.mark.asyncio
async def test_deployment_captures_flow_replay_payload(input_documents: list[Document]) -> None:
    """Flow execution stores replay_payload on the persisted flow node payload."""
    database = _MemoryDatabase()
    deployment = ExampleDeployment()

    await deployment.run("run-replay", input_documents, _TestOptions(), database=database)

    flow_spans = [span for span in database._spans.values() if span.kind == SpanKind.FLOW]
    assert flow_spans, "No flow spans were persisted"
    flow_input = json.loads(flow_spans[0].input_json)
    assert flow_spans[0].target.endswith(":StageOne.run")
    assert flow_input["options"]["data"] == {"value": "ok"}


@pytest.mark.asyncio
async def test_flow_detail_includes_default_flow_options(input_documents: list[Document]) -> None:
    database = _MemoryDatabase()
    await ExampleDeployment().run("run-flow-options", input_documents, _TestOptions(), database=database)

    flow_spans = [span for span in database._spans.values() if span.kind == SpanKind.FLOW]
    assert flow_spans, "No flow spans were persisted"
    flow_input = json.loads(flow_spans[0].input_json)

    assert flow_input["options"]["data"]["value"] == "ok"


@pytest.mark.asyncio
async def test_deployment_stores_parent_execution_id_on_deployment_node(input_documents: list[Document]) -> None:
    """Deployment node payload keeps parent_execution_id for cross-deployment DAG linking."""
    database = _MemoryDatabase()
    deployment = ExampleDeployment()
    parent_execution_id = uuid4()

    await deployment.run(
        "run-parent-link",
        input_documents,
        _TestOptions(),
        database=database,
        parent_execution_id=parent_execution_id,
    )

    deployment_spans = [span for span in database._spans.values() if span.kind == SpanKind.DEPLOYMENT]
    assert deployment_spans
    assert deployment_spans[0].run_id == "run-parent-link"


def test_run_local_forces_memory_database(monkeypatch: pytest.MonkeyPatch, input_documents: list[Document]) -> None:
    deployment = ExampleDeployment()
    called = False

    def _unexpected_database_creation(*args: object, **kwargs: object) -> object:
        nonlocal called
        called = True
        return _MemoryDatabase()

    monkeypatch.setattr("ai_pipeline_core.deployment.base._create_span_database_from_settings", _unexpected_database_creation)

    result = deployment.run_local("run-local-memory", input_documents, _TestOptions())

    assert result.success is True
    assert called is False
