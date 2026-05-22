"""Tests for deployment helper functions."""

# pyright: reportPrivateUsage=false

import pytest

from ai_pipeline_core import (
    FlowOptions,
    PipelineDeployment,
    RemoteDeployment,
)
from ai_pipeline_core.deployment import DeploymentResult
from ai_pipeline_core.deployment._helpers import (
    MAX_RUN_ID_LENGTH,
    class_name_to_deployment_name,
    extract_generic_params,
    validate_run_id,
)


class TestClassNameToDeploymentName:
    """Test PascalCase to kebab-case conversion."""

    def test_simple_name(self):
        """Test simple two-word name."""
        assert class_name_to_deployment_name("ResearchPipeline") == "research-pipeline"

    def test_single_word(self):
        """Test single word name."""
        assert class_name_to_deployment_name("Pipeline") == "pipeline"

    def test_multi_word(self):
        """Test multi-word name."""
        assert class_name_to_deployment_name("MyLongPipelineName") == "my-long-pipeline-name"

    def test_acronym(self):
        """Test name with consecutive capitals."""
        assert class_name_to_deployment_name("AIResearch") == "a-i-research"


# --- Test infrastructure for extract_generic_params ---


class SampleResult(DeploymentResult):
    """Test result type."""

    report: str = ""


class SampleRemote(RemoteDeployment[FlowOptions, SampleResult]):
    """Remote deployment for testing extract_generic_params."""


class TestExtractGenericParams:
    """Test extraction of generic type parameters from Generic subclasses."""

    def test_extracts_remote_deployment_params(self):
        """Test correct extraction from RemoteDeployment subclass (2 params)."""
        params = extract_generic_params(SampleRemote, RemoteDeployment)
        assert len(params) == 2
        assert params[0] is FlowOptions
        assert params[1] is SampleResult

    def test_returns_empty_for_non_generic(self):
        """Test returns empty tuple for class without generic params."""

        class Plain:
            """Plain class."""

        result = extract_generic_params(Plain, PipelineDeployment)
        assert result == ()


# ---------------------------------------------------------------------------
# Tests for validate_run_id
# ---------------------------------------------------------------------------


class TestValidateRunId:
    """Tests for run_id format validation."""

    def test_accepts_alphanumeric(self):
        validate_run_id("research123")

    def test_accepts_underscores(self):
        validate_run_id("my_run_id")

    def test_accepts_hyphens(self):
        validate_run_id("my-run-id")

    def test_accepts_mixed(self):
        validate_run_id("project-A_run-2024-01-15_abc123")

    def test_accepts_single_char(self):
        validate_run_id("x")

    def test_accepts_max_length(self):
        validate_run_id("a" * MAX_RUN_ID_LENGTH)

    def test_rejects_empty(self):
        with pytest.raises(ValueError, match="must not be empty"):
            validate_run_id("")

    def test_rejects_over_max_length(self):
        with pytest.raises(ValueError, match=f"max is {MAX_RUN_ID_LENGTH}"):
            validate_run_id("a" * (MAX_RUN_ID_LENGTH + 1))

    def test_rejects_spaces(self):
        with pytest.raises(ValueError, match="invalid characters"):
            validate_run_id("my run")

    def test_rejects_dots(self):
        with pytest.raises(ValueError, match="invalid characters"):
            validate_run_id("v1.0.0")

    def test_rejects_slashes(self):
        with pytest.raises(ValueError, match="invalid characters"):
            validate_run_id("path/to/run")

    def test_rejects_colons(self):
        with pytest.raises(ValueError, match="invalid characters"):
            validate_run_id("run:scope")

    def test_rejects_at_sign(self):
        with pytest.raises(ValueError, match="invalid characters"):
            validate_run_id("user@host")
