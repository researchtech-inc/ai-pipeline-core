"""Tests for RemoteDeployment class.

Covers __init_subclass__ validation, run() behavior, inline/remote mode,
deployment_class resolution, and edge cases.
"""

# pyright: reportPrivateUsage=false

import json
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4, uuid7

import pytest
from pydantic import BaseModel

from ai_pipeline_core import DeploymentResult, Document, FlowOptions, PipelineDeployment
from ai_pipeline_core.database import SpanKind
from ai_pipeline_core.database._memory import _MemoryDatabase
from ai_pipeline_core.database.filesystem._backend import FilesystemDatabase
from ai_pipeline_core.deployment._helpers import class_name_to_deployment_name, extract_generic_params
from ai_pipeline_core.deployment._resolve import _DocumentInput
from ai_pipeline_core.deployment.remote import RemoteDeployment, _derive_remote_run_id
from ai_pipeline_core.pipeline import PipelineFlow, pipeline_test_context
from ai_pipeline_core.pipeline._runtime_sinks import build_runtime_sinks
from ai_pipeline_core.settings import settings


# ---------------------------------------------------------------------------
# Test document/result types
# ---------------------------------------------------------------------------


class AlphaDoc(Document):
    """First test document type."""


class BetaDoc(Document):
    """Second test document type."""


class GammaDoc(Document):
    """Third test document type."""


class SimpleResult(DeploymentResult):
    report: str = ""


class NestedDocResult(DeploymentResult):
    """Result containing a Document field — tests nested deserialization."""

    output_doc: AlphaDoc


class InlineRemoteChildOutput(Document):
    """Output doc for inline remote deployment tests."""


class InlineRemoteChildResult(DeploymentResult):
    output_doc: InlineRemoteChildOutput


class InlineRemoteChildFlow(PipelineFlow):
    async def run(self, alpha_docs: tuple[AlphaDoc, ...], options: FlowOptions) -> tuple[InlineRemoteChildOutput, ...]:
        _ = options
        return (InlineRemoteChildOutput.derive(derived_from=alpha_docs, name="remote.txt", content="remote"),)


class InlineRemoteChildDeployment(PipelineDeployment[FlowOptions, InlineRemoteChildResult]):
    flow_retries = 0

    def build_flows(self, options: FlowOptions) -> list[PipelineFlow]:
        _ = options
        return [InlineRemoteChildFlow()]

    @staticmethod
    def build_result(run_id: str, documents: tuple[Document, ...], options: FlowOptions) -> InlineRemoteChildResult:
        _ = (run_id, options)
        outputs = [document for document in documents if isinstance(document, InlineRemoteChildOutput)]
        if not outputs:
            raise RuntimeError("InlineRemoteChildDeployment expected an InlineRemoteChildOutput artifact.")
        return InlineRemoteChildResult(success=True, output_doc=outputs[-1])


# ===================================================================
# 1. __init_subclass__ validation
# ===================================================================


class TestNameDerivation:
    def test_auto_derives_kebab_case(self):
        class MyResearchPipeline(RemoteDeployment[FlowOptions, SimpleResult]):
            pass

        assert MyResearchPipeline.name == "my-research-pipeline"

    def test_single_word(self):
        class Research(RemoteDeployment[FlowOptions, SimpleResult]):
            pass

        assert Research.name == "research"

    def test_explicit_name_override(self):
        class CustomNamed(RemoteDeployment[FlowOptions, SimpleResult]):
            name = "my-explicit-name"

        assert CustomNamed.name == "my-explicit-name"

    def test_auto_derived_matches_pipeline_deployment_convention(self):
        class SamplePipeline(RemoteDeployment[FlowOptions, SimpleResult]):
            pass

        assert SamplePipeline.name == class_name_to_deployment_name("SamplePipeline")


class TestGenericExtraction:
    def test_extracts_options_type(self):
        class Foo(RemoteDeployment[FlowOptions, SimpleResult]):
            pass

        assert Foo.options_type is FlowOptions

    def test_extracts_result_type(self):
        class Foo(RemoteDeployment[FlowOptions, SimpleResult]):
            pass

        assert Foo.result_type is SimpleResult

    def test_two_args_returned_by_helper(self):
        class Foo(RemoteDeployment[FlowOptions, SimpleResult]):
            pass

        args = extract_generic_params(Foo, RemoteDeployment)
        assert len(args) == 2
        assert args[0] is FlowOptions
        assert args[1] is SimpleResult


class TestTOptionsValidation:
    def test_rejects_non_flow_options(self):
        class NotFlowOptions(BaseModel):
            x: int = 1

        with pytest.raises(TypeError, match="FlowOptions subclass"):

            class Bad(RemoteDeployment[NotFlowOptions, SimpleResult]):  # type: ignore[type-var]
                pass

    def test_accepts_flow_options_subclass(self):
        class CustomOpts(FlowOptions):
            budget: float = 10.0

        class Good(RemoteDeployment[CustomOpts, SimpleResult]):
            pass

        assert Good.options_type is CustomOpts


class TestTResultValidation:
    def test_rejects_non_deployment_result(self):
        class NotAResult(BaseModel):
            x: int = 1

        with pytest.raises(TypeError, match="DeploymentResult subclass"):

            class Bad(RemoteDeployment[FlowOptions, NotAResult]):  # type: ignore[type-var]
                pass

    def test_accepts_deployment_result_subclass(self):
        class Foo(RemoteDeployment[FlowOptions, SimpleResult]):
            pass

        assert Foo.result_type is SimpleResult


class TestMissingGenerics:
    def test_rejects_no_generic_params(self):
        with pytest.raises(TypeError, match="must specify 2 Generic parameters"):

            class Bad(RemoteDeployment):  # type: ignore[type-arg]
                pass

    def test_defaults_result_type_when_one_generic_param_is_provided(self):
        class Defaulted(RemoteDeployment[FlowOptions]):
            pass

        assert Defaulted.result_type is DeploymentResult


# ===================================================================
# 2. deployment_path property
# ===================================================================


class TestDeploymentPath:
    def test_auto_derived(self):
        class AiResearch(RemoteDeployment[FlowOptions, SimpleResult]):
            pass

        assert AiResearch().deployment_path == "ai-research/ai_research"

    def test_explicit_name(self):
        class CustomName(RemoteDeployment[FlowOptions, SimpleResult]):
            name = "my-pipeline"

        assert CustomName().deployment_path == "my-pipeline/my_pipeline"

    def test_path_format_matches_deployer(self):
        class SamplePipeline(RemoteDeployment[FlowOptions, SimpleResult]):
            pass

        path = SamplePipeline().deployment_path
        flow_name, deployment_name = path.split("/")
        assert flow_name == "sample-pipeline"
        assert deployment_name == "sample_pipeline"
        assert "-" not in deployment_name


# ===================================================================
# 3. deployment_class and _resolve_deployment_class
# ===================================================================


class TestDeploymentClass:
    def test_default_deployment_class_is_empty(self):
        class Foo(RemoteDeployment[FlowOptions, SimpleResult]):
            pass

        assert Foo.deployment_class == ""

    def test_deployment_class_set_inline(self):
        class Foo(RemoteDeployment[FlowOptions, SimpleResult]):
            deployment_class = "my_module:MyDeployment"

        assert Foo.deployment_class == "my_module:MyDeployment"


class TestResolveDeploymentClass:
    def test_raises_when_deployment_class_not_set(self):
        class Foo(RemoteDeployment[FlowOptions, SimpleResult]):
            pass

        with pytest.raises(ValueError, match="deployment_class is not set"):
            Foo()._resolve_deployment_class()

    def test_imports_class_from_module_path(self):
        class Foo(RemoteDeployment[FlowOptions, SimpleResult]):
            deployment_class = "ai_pipeline_core.deployment.base:DeploymentResult"

        resolved = Foo()._resolve_deployment_class()
        assert resolved is DeploymentResult

    def test_raises_on_invalid_module(self):
        class Foo(RemoteDeployment[FlowOptions, SimpleResult]):
            deployment_class = "nonexistent.module:SomeClass"

        with pytest.raises(ModuleNotFoundError):
            Foo()._resolve_deployment_class()

    def test_raises_on_invalid_class_name(self):
        class Foo(RemoteDeployment[FlowOptions, SimpleResult]):
            deployment_class = "ai_pipeline_core.deployment.base:NonexistentClass"

        with pytest.raises(AttributeError):
            Foo()._resolve_deployment_class()


# ===================================================================
# 4. run() — inline vs remote mode detection
# ===================================================================


_EXEC_CTX_PATH = "ai_pipeline_core.deployment.remote.get_execution_context"


class TestInlineModeDetection:
    """run() uses inline mode when the active backend does not support remote execution."""

    async def test_inline_mode_with_memory_database(self):
        """MemoryDatabase triggers inline mode."""

        class Foo(RemoteDeployment[FlowOptions, SimpleResult]):
            pass

        mock_ctx = MagicMock()
        mock_ctx.database = _MemoryDatabase()
        mock_ctx.deployment_id = uuid4()
        mock_ctx.root_deployment_id = uuid4()
        mock_ctx.current_span_id = uuid4()
        mock_ctx.deployment_name = "test"
        mock_ctx.next_child_sequence.return_value = 0

        foo = Foo()
        with (
            patch(_EXEC_CTX_PATH, return_value=mock_ctx),
            patch.object(Foo, "_run_inline", new_callable=AsyncMock) as mock_inline,
            patch.object(Foo, "_run_remote", new_callable=AsyncMock),
        ):
            mock_inline.return_value = SimpleResult(success=True)
            with pipeline_test_context(run_id="project"):
                await foo.run((), FlowOptions())

        mock_inline.assert_awaited_once()

    async def test_inline_mode_with_filesystem_database(self, tmp_path):
        """FilesystemDatabase triggers inline mode via supports_remote=False."""

        class Foo(RemoteDeployment[FlowOptions, SimpleResult]):
            pass

        mock_ctx = MagicMock()
        mock_ctx.database = FilesystemDatabase(tmp_path)
        mock_ctx.deployment_id = uuid4()
        mock_ctx.root_deployment_id = uuid4()
        mock_ctx.current_span_id = uuid4()
        mock_ctx.deployment_name = "test"
        mock_ctx.next_child_sequence.return_value = 0

        foo = Foo()
        with (
            patch(_EXEC_CTX_PATH, return_value=mock_ctx),
            patch.object(Foo, "_run_inline", new_callable=AsyncMock) as mock_inline,
            patch.object(Foo, "_run_remote", new_callable=AsyncMock) as mock_remote,
        ):
            mock_inline.return_value = SimpleResult(success=True)
            with pipeline_test_context(run_id="project"):
                await foo.run((), FlowOptions())

        mock_inline.assert_awaited_once()
        mock_remote.assert_not_awaited()

    async def test_deployment_class_forces_inline_even_with_remote_database(self):
        """When deployment_class is set, inline mode must be used regardless of database capability."""

        class FooWithClass(RemoteDeployment[FlowOptions, SimpleResult]):
            deployment_class = "some.module:SomeDeployment"

        class RemoteCapableDatabase:
            supports_remote = True

            async def insert_span(self, _span):
                return None

        mock_ctx = MagicMock()
        mock_ctx.database = RemoteCapableDatabase()
        mock_ctx.deployment_id = uuid4()
        mock_ctx.root_deployment_id = uuid4()
        mock_ctx.current_span_id = uuid4()
        mock_ctx.deployment_name = "test"
        mock_ctx.next_child_sequence.return_value = 0

        foo = FooWithClass()
        with (
            patch(_EXEC_CTX_PATH, return_value=mock_ctx),
            patch.object(FooWithClass, "_run_inline", new_callable=AsyncMock) as mock_inline,
            patch.object(FooWithClass, "_run_remote", new_callable=AsyncMock) as mock_remote,
        ):
            mock_inline.return_value = SimpleResult(success=True)
            with pipeline_test_context(run_id="project"):
                await foo.run((), FlowOptions())

        mock_inline.assert_awaited_once()
        mock_remote.assert_not_awaited()

    async def test_remote_mode_with_remote_capable_database(self):
        """Backends advertising supports_remote=True use Prefect remote execution."""

        class Foo(RemoteDeployment[FlowOptions, SimpleResult]):
            pass

        class RemoteCapableDatabase:
            supports_remote = True

            async def insert_span(self, _span):
                return None

        mock_ctx = MagicMock()
        mock_ctx.database = RemoteCapableDatabase()
        mock_ctx.deployment_id = uuid4()
        mock_ctx.root_deployment_id = uuid4()
        mock_ctx.current_span_id = uuid4()
        mock_ctx.deployment_name = "test"
        mock_ctx.next_child_sequence.return_value = 0

        foo = Foo()
        with (
            patch(_EXEC_CTX_PATH, return_value=mock_ctx),
            patch.object(Foo, "_run_inline", new_callable=AsyncMock) as mock_inline,
            patch.object(Foo, "_run_remote", new_callable=AsyncMock) as mock_remote,
        ):
            mock_remote.return_value = SimpleResult(success=True)
            with pipeline_test_context(run_id="project"):
                await foo.run((), FlowOptions())

        mock_remote.assert_awaited_once()
        mock_inline.assert_not_awaited()

    async def test_inline_mode_with_no_context_logs_warning(self, caplog):
        """A remote deployment falling back to inline execution must emit a warning."""

        class Foo(RemoteDeployment[FlowOptions, SimpleResult]):
            pass

        foo = Foo()
        with (
            patch(_EXEC_CTX_PATH, return_value=None),
            patch.object(Foo, "_run_inline", new_callable=AsyncMock) as mock_inline,
        ):
            mock_inline.return_value = SimpleResult(success=True)
            with pipeline_test_context(run_id="project"):
                await foo.run((), FlowOptions())

        assert "inline" in caplog.text.lower()
        mock_inline.assert_awaited_once()

    async def test_inline_mode_with_no_context(self):
        """When no execution context exists, database is None => inline mode."""

        class Foo(RemoteDeployment[FlowOptions, SimpleResult]):
            pass

        foo = Foo()
        with (
            patch(_EXEC_CTX_PATH, return_value=None),
            patch.object(Foo, "_run_inline", new_callable=AsyncMock) as mock_inline,
        ):
            mock_inline.return_value = SimpleResult(success=True)
            with pipeline_test_context(run_id="project"):
                await foo.run((), FlowOptions())

        mock_inline.assert_awaited_once()

    async def test_inline_mode_propagates_publisher_and_parent_execution_id(self):
        """Inline remote execution should preserve lineage and publisher from the active context."""

        class Foo(RemoteDeployment[FlowOptions, SimpleResult]):
            pass

        publisher = MagicMock()
        execution_id = uuid4()
        mock_ctx = MagicMock()
        mock_ctx.database = _MemoryDatabase()
        mock_ctx.deployment_id = uuid4()
        mock_ctx.root_deployment_id = uuid4()
        mock_ctx.current_span_id = uuid4()
        mock_ctx.deployment_name = "test"
        mock_ctx.publisher = publisher
        mock_ctx.execution_id = execution_id
        mock_ctx.next_child_sequence.return_value = 0

        foo = Foo()
        with (
            patch(_EXEC_CTX_PATH, return_value=mock_ctx),
            patch.object(Foo, "_run_inline", new_callable=AsyncMock) as mock_inline,
        ):
            mock_inline.return_value = SimpleResult(success=True)
            with pipeline_test_context(run_id="project"):
                await foo.run((), FlowOptions())

        assert mock_inline.await_args.kwargs["publisher"] is publisher
        assert mock_inline.await_args.kwargs["parent_execution_id"] == execution_id

    async def test_inline_remote_persists_bidirectional_remote_linkage(self):
        class Foo(RemoteDeployment[FlowOptions, InlineRemoteChildResult]):
            deployment_class = f"{__name__}:InlineRemoteChildDeployment"

        doc = AlphaDoc.create_root(name="test.txt", content="hello", reason="test input")
        database = _MemoryDatabase()

        with pipeline_test_context(run_id="project") as ctx:
            ctx.database = database
            ctx.sinks = build_runtime_sinks(database=database, settings_obj=settings).span_sinks
            ctx.deployment_id = uuid7()
            ctx.root_deployment_id = ctx.deployment_id
            ctx.current_span_id = ctx.deployment_id
            ctx.current_span_id = ctx.deployment_id
            ctx.deployment_name = "parent-deployment"

            result = await Foo().run((doc,), FlowOptions())

        expected_output = InlineRemoteChildOutput.derive(derived_from=(doc,), name="remote.txt", content="remote")
        assert result.output_doc.sha256 == expected_output.sha256

        task_spans = [span for span in database._spans.values() if span.kind == SpanKind.TASK]
        deployment_spans = [span for span in database._spans.values() if span.kind == SpanKind.DEPLOYMENT]

        assert len(task_spans) == 1
        assert len(deployment_spans) == 1

        task_meta = json.loads(task_spans[0].meta_json)
        task_input = json.loads(task_spans[0].input_json)

        assert task_spans[0].name == f"remote:{Foo.name}"
        assert task_meta["deployment_name"] == "parent-deployment"
        assert task_meta["remote_mode"] == "inline"
        assert task_input["options"]["data"] == {}
        assert deployment_spans[0].parent_span_id == task_spans[0].span_id


# ===================================================================
# 5. run() validation
# ===================================================================


class TestRunValidation:
    async def test_rejects_outside_execution_context(self):
        class Foo(RemoteDeployment[FlowOptions, SimpleResult]):
            pass

        with pytest.raises(RuntimeError, match="pipeline_test_context"):
            await Foo().run((), FlowOptions())

    async def test_rejects_invalid_run_id(self):
        class Foo(RemoteDeployment[FlowOptions, SimpleResult]):
            pass

        with pytest.raises(ValueError, match="contains invalid characters"):
            with pipeline_test_context(run_id="invalid run id with spaces"):
                await Foo().run((), FlowOptions())

    async def test_rejects_empty_run_id(self):
        class Foo(RemoteDeployment[FlowOptions, SimpleResult]):
            pass

        with pytest.raises(ValueError, match="must not be empty"):
            with pipeline_test_context(run_id=""):
                await Foo().run((), FlowOptions())

    async def test_rejects_too_long_derived_run_id(self):
        """A 92+ char base run_id produces a derived run_id exceeding 100 chars."""

        class Foo(RemoteDeployment[FlowOptions, SimpleResult]):
            pass

        doc = AlphaDoc.create_root(name="test.txt", content="hello", reason="test")
        long_run_id = "a" * 92  # 92 + 1 ('-') + 8 (fingerprint) = 101 > 100
        with pytest.raises(ValueError, match="Shorten the base run_id"):
            with pipeline_test_context(run_id=long_run_id):
                await Foo().run((doc,), FlowOptions())


# ===================================================================
# 6. run() result handling
# ===================================================================


_RUN_REMOTE = "ai_pipeline_core.deployment.remote._run_remote_deployment"


class TestRunResultDeserialization:
    async def test_run_remote_uses_document_inputs_parameter_name(self):
        """Remote calls must match the Prefect wrapper's document_inputs parameter."""

        class Foo(RemoteDeployment[FlowOptions, SimpleResult]):
            pass

        doc = AlphaDoc.create_root(name="test.txt", content="hello", reason="test input")
        foo = Foo()
        with patch(_RUN_REMOTE) as mock_prefect:
            mock_prefect.return_value = {"success": True, "report": "from dict"}
            await foo._run_remote(
                "project",
                [doc],
                FlowOptions(),
                root_deployment_id=uuid4(),
                parent_deployment_task_id=uuid4(),
            )

        parameters = mock_prefect.await_args.args[1]
        assert "document_inputs" in parameters
        assert "documents" not in parameters

    async def test_run_remote_normalizes_document_payloads_to_document_input_schema(self):
        """Remote parameters must be serialized as DocumentInput-compatible payloads."""

        class Foo(RemoteDeployment[FlowOptions, SimpleResult]):
            pass

        doc = AlphaDoc.create_root(name="test.txt", content="hello", reason="test input")
        foo = Foo()
        with patch(_RUN_REMOTE) as mock_prefect:
            mock_prefect.return_value = {"success": True, "report": "from dict"}
            await foo._run_remote(
                "project",
                [doc],
                FlowOptions(),
                root_deployment_id=uuid4(),
                parent_deployment_task_id=uuid4(),
            )

        parameters = mock_prefect.await_args.args[1]
        serialized = parameters["document_inputs"][0]
        validated = _DocumentInput.model_validate(serialized)
        assert validated.class_name == "AlphaDoc"
        assert "sha256" not in serialized

    async def test_run_remote_uses_parent_execution_id_in_parameters(self):
        """Remote Prefect calls should receive the caller execution_id, not the root deployment id."""

        class Foo(RemoteDeployment[FlowOptions, SimpleResult]):
            pass

        parent_execution_id = uuid4()
        root_deployment_id = uuid4()
        foo = Foo()
        with patch(_RUN_REMOTE) as mock_prefect:
            mock_prefect.return_value = {"success": True, "report": "from dict"}
            await foo._run_remote(
                "project",
                [],
                FlowOptions(),
                root_deployment_id=root_deployment_id,
                parent_deployment_task_id=uuid4(),
                parent_execution_id=parent_execution_id,
            )

        parameters = mock_prefect.await_args.args[1]
        assert parameters["parent_execution_id"] == str(parent_execution_id)
        assert parameters["root_deployment_id"] == str(root_deployment_id)

    async def test_dict_result_deserialized_in_run_remote(self):
        """_run_remote deserializes dict results via result_type.model_validate."""

        class Foo(RemoteDeployment[FlowOptions, SimpleResult]):
            pass

        foo = Foo()
        with patch(_RUN_REMOTE) as mock_prefect:
            mock_prefect.return_value = {"success": True, "report": "from dict"}
            result = await foo._run_remote(
                "project",
                [],
                FlowOptions(),
                root_deployment_id=uuid4(),
                parent_deployment_task_id=uuid4(),
            )

        assert isinstance(result, SimpleResult)
        assert result.report == "from dict"

    async def test_document_result_round_trip_preserves_sha256(self):
        """Document-valued results hydrate through the dict path with identical sha256."""

        class Foo(RemoteDeployment[FlowOptions, NestedDocResult]):
            pass

        output_doc = AlphaDoc.create_root(name="nested.txt", content="nested", reason="test input")
        foo = Foo()
        with patch(_RUN_REMOTE) as mock_prefect:
            mock_prefect.return_value = NestedDocResult(success=True, output_doc=output_doc).model_dump(mode="json")
            result = await foo._run_remote(
                "project",
                [],
                FlowOptions(),
                root_deployment_id=uuid4(),
                parent_deployment_task_id=uuid4(),
            )

        assert isinstance(result, NestedDocResult)
        assert result.output_doc.sha256 == output_doc.sha256

    async def test_deployment_result_returned_directly_in_run_remote(self):
        """_run_remote returns DeploymentResult instances directly."""

        class Foo(RemoteDeployment[FlowOptions, SimpleResult]):
            pass

        expected = SimpleResult(success=True, report="direct")
        foo = Foo()
        with patch(_RUN_REMOTE) as mock_prefect:
            mock_prefect.return_value = expected
            result = await foo._run_remote(
                "project",
                [],
                FlowOptions(),
                root_deployment_id=uuid4(),
                parent_deployment_task_id=uuid4(),
            )

        assert result is expected

    async def test_invalid_result_type_raises_in_run_remote(self):
        class Foo(RemoteDeployment[FlowOptions, SimpleResult]):
            pass

        foo = Foo()
        with patch(_RUN_REMOTE) as mock_prefect:
            mock_prefect.return_value = "invalid string"
            with pytest.raises(TypeError, match="unexpected type"):
                await foo._run_remote(
                    "project",
                    [],
                    FlowOptions(),
                    root_deployment_id=uuid4(),
                    parent_deployment_task_id=uuid4(),
                )


class TestRunErrorPropagation:
    async def test_error_propagates_from_run_remote(self):
        class Foo(RemoteDeployment[FlowOptions, SimpleResult]):
            pass

        foo = Foo()
        with patch(_RUN_REMOTE) as mock_prefect:
            mock_prefect.side_effect = ValueError("deployment not found")
            with pytest.raises(ValueError, match="deployment not found"):
                await foo._run_remote(
                    "project",
                    [],
                    FlowOptions(),
                    root_deployment_id=uuid4(),
                    parent_deployment_task_id=uuid4(),
                )


# ===================================================================
# 7. Serialization round-trip
# ===================================================================


class TestSerializationRoundTrip:
    def test_serialize_model_produces_reconstructable_dict(self):
        doc = AlphaDoc.create_root(name="test.txt", content="hello", reason="test input")
        serialized = doc.serialize_model()

        assert serialized["class_name"] == "AlphaDoc"
        assert serialized["sha256"] == doc.sha256

        restored = AlphaDoc.model_validate(serialized)
        assert isinstance(restored, AlphaDoc)
        assert restored.sha256 == doc.sha256

    def test_model_validate_round_trip_with_known_types(self):
        alpha = AlphaDoc.create_root(name="a.txt", content="alpha content", reason="test input")
        beta = BetaDoc.create_root(name="b.txt", content="beta content", reason="test input")

        type_map = {cls.__name__: cls for cls in [AlphaDoc, BetaDoc]}
        for original in [alpha, beta]:
            serialized = original.serialize_model()
            cls = type_map[serialized["class_name"]]
            restored = cls.model_validate(serialized)
            assert type(restored) is type(original)
            assert restored.sha256 == original.sha256

    async def test_serialize_document_inputs_roundtrip_preserves_provenance(self):
        """Full remote serialization path: document with provenance survives _serialize → resolve."""
        from ai_pipeline_core.deployment._resolve import resolve_document_inputs
        from ai_pipeline_core.deployment.remote import _serialize_document_inputs

        root = AlphaDoc.create_root(name="root.txt", content="root", reason="test")
        derived = AlphaDoc.derive(derived_from=(root,), name="derived.txt", content="derived")
        trigger_source = BetaDoc.create_root(name="trigger.txt", content="trigger", reason="test")
        triggered = AlphaDoc.create(triggered_by=(trigger_source,), name="triggered.txt", content="triggered")

        for original in [derived, triggered]:
            serialized_dicts = _serialize_document_inputs([original])
            doc_inputs = [_DocumentInput.model_validate(d) for d in serialized_dicts]
            resolved = await resolve_document_inputs(doc_inputs, [AlphaDoc, BetaDoc])
            assert len(resolved) == 1
            assert resolved[0].derived_from == original.derived_from, (
                f"derived_from lost for {original.name}: expected {original.derived_from}, got {resolved[0].derived_from}"
            )
            assert resolved[0].triggered_by == original.triggered_by, (
                f"triggered_by lost for {original.name}: expected {original.triggered_by}, got {resolved[0].triggered_by}"
            )

    async def test_remote_path_preserves_provenance_on_derived_documents(self):
        """The remote Prefect path (_run_remote) must preserve provenance on input documents.

        Simulates what happens when RemoteDeployment._run_remote serializes documents
        via _serialize_document_inputs and the worker resolves them via resolve_document_inputs.
        This is the exact path that breaks: parent creates derived documents, sends them
        as Prefect parameters, and the worker rejects them.
        """
        from ai_pipeline_core.deployment._resolve import resolve_document_inputs
        from ai_pipeline_core.deployment.remote import _serialize_document_inputs

        root = AlphaDoc.create_root(name="root.txt", content="root content", reason="test")
        derived_input = AlphaDoc.derive(derived_from=(root,), name="derived.txt", content="derived content")

        # This is what _run_remote does (remote.py:321)
        serialized = _serialize_document_inputs([derived_input])

        # This is what the Prefect worker does (_prefect.py:81-85)
        doc_inputs = [_DocumentInput.model_validate(s) for s in serialized]
        resolved = await resolve_document_inputs(doc_inputs, [AlphaDoc])

        assert len(resolved) == 1
        assert resolved[0].derived_from == derived_input.derived_from
        assert resolved[0].name == "derived.txt"


# ===================================================================
# 8. Edge cases
# ===================================================================


class TestEdgeCases:
    def test_module_level_instantiation_no_event_loop(self):
        class Foo(RemoteDeployment[FlowOptions, SimpleResult]):
            pass

        instance = Foo()
        assert instance.name == "foo"
        assert instance.deployment_path == "foo/foo"

    def test_multiple_instances_share_class_state(self):
        class Foo(RemoteDeployment[FlowOptions, SimpleResult]):
            pass

        a = Foo()
        b = Foo()
        assert a.name == b.name
        assert a.deployment_path == b.deployment_path
        assert a.result_type is b.result_type


# ===================================================================
# 9. extract_generic_params helper
# ===================================================================


class TestExtractGenericParams:
    def test_two_params_from_remote_deployment(self):
        class Foo(RemoteDeployment[FlowOptions, SimpleResult]):
            pass

        result = extract_generic_params(Foo, RemoteDeployment)
        assert len(result) == 2
        assert result[0] is FlowOptions
        assert result[1] is SimpleResult

    def test_no_match_returns_empty_tuple(self):
        result = extract_generic_params(SimpleResult, RemoteDeployment)
        assert result == ()


# ===================================================================
# 10. Deterministic remote run_id
# ===================================================================


class TestDeriveRemoteRunId:
    """Tests for deterministic run_id generation from inputs."""

    def test_deterministic_same_inputs(self):
        """Same documents + options produce same derived run_id."""
        doc = AlphaDoc.create_root(name="test.txt", content="hello", reason="test")
        opts = FlowOptions()
        id1 = _derive_remote_run_id("base-run", [doc], opts)
        id2 = _derive_remote_run_id("base-run", [doc], opts)
        assert id1 == id2

    def test_different_docs_different_id(self):
        """Different document content produces different derived run_id."""
        doc_a = AlphaDoc.create_root(name="a.txt", content="aaa", reason="test")
        doc_b = AlphaDoc.create_root(name="b.txt", content="bbb", reason="test")
        opts = FlowOptions()
        id_a = _derive_remote_run_id("base-run", [doc_a], opts)
        id_b = _derive_remote_run_id("base-run", [doc_b], opts)
        assert id_a != id_b

    def test_different_options_different_id(self):
        """Different options produce different derived run_id."""
        doc = AlphaDoc.create_root(name="test.txt", content="hello", reason="test")

        class CustomOptions(FlowOptions):
            budget: float = 10.0

        opts1 = CustomOptions(budget=10.0)
        opts2 = CustomOptions(budget=20.0)
        id1 = _derive_remote_run_id("base-run", [doc], opts1)
        id2 = _derive_remote_run_id("base-run", [doc], opts2)
        assert id1 != id2

    @pytest.mark.ai_docs
    def test_format_starts_with_base_run_id(self):
        """Derived run_id starts with the user's base run_id."""
        doc = AlphaDoc.create_root(name="test.txt", content="hello", reason="test")
        derived = _derive_remote_run_id("my-project", [doc], FlowOptions())
        assert derived.startswith("my-project-")

    def test_format_alphanumeric_hyphen(self):
        """Derived run_id matches the allowed pattern."""
        import re

        doc = AlphaDoc.create_root(name="test.txt", content="hello", reason="test")
        derived = _derive_remote_run_id("run-123", [doc], FlowOptions())
        assert re.match(r"^[a-zA-Z0-9_-]+$", derived)

    def test_empty_docs_deterministic(self):
        """Empty documents list still produces deterministic run_id."""
        opts = FlowOptions()
        id1 = _derive_remote_run_id("base", [], opts)
        id2 = _derive_remote_run_id("base", [], opts)
        assert id1 == id2
        assert id1.startswith("base-")

    def test_different_base_run_ids(self):
        """Different base run_id produces different derived run_id even with same inputs."""
        doc = AlphaDoc.create_root(name="test.txt", content="hello", reason="test")
        opts = FlowOptions()
        id_a = _derive_remote_run_id("project-a", [doc], opts)
        id_b = _derive_remote_run_id("project-b", [doc], opts)
        assert id_a != id_b

    def test_cli_fields_excluded_from_fingerprint(self):
        """CLI-specific fields (working_directory, start, end) don't affect the derived run_id."""
        from pathlib import Path

        class CliOptions(FlowOptions):
            working_directory: Path = Path(".")
            start: str = ""
            budget: float = 10.0

        doc = AlphaDoc.create_root(name="test.txt", content="hello", reason="test")
        id_a = _derive_remote_run_id("run", [doc], CliOptions(working_directory=Path("/home/user/project-a")))
        id_b = _derive_remote_run_id("run", [doc], CliOptions(working_directory=Path("/other/path")))
        assert id_a == id_b, "working_directory is a CLI field and must not affect the fingerprint"

        id_c = _derive_remote_run_id("run", [doc], CliOptions(budget=20.0))
        assert id_a != id_c, "non-CLI fields must affect the fingerprint"


# ===================================================================
# 11. Bug reproduction tests
# ===================================================================


class TestReportedBugs:
    """Tests to prove/disprove bugs found during code review."""

    def test_typing_union_syntax(self):
        """Bug report: typing.Union[A, B] is rejected by _validate_document_type.

        Project bans Union syntax per CLAUDE.md, but should it raise a clear error?
        """
        # RemoteDeployment now takes 2 params (options, result), no doc type param
        # This test documents that the class accepts valid 2-param generics

        class UnionSyntax(RemoteDeployment[FlowOptions, SimpleResult]):
            pass

        assert UnionSyntax.options_type is FlowOptions
        assert UnionSyntax.result_type is SimpleResult
