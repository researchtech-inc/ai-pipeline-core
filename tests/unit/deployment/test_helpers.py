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
    MAX_LABEL_KEY_LENGTH,
    MAX_LABEL_VALUE_BYTES,
    MAX_LABELS,
    MAX_RUN_ID_LENGTH,
    class_name_to_deployment_name,
    extract_generic_params,
    validate_labels,
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

    def test_rejects_trailing_newline(self):
        with pytest.raises(ValueError, match="invalid characters"):
            validate_run_id("run\n")

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


class TestValidateLabels:
    """Tests for deployment label validation."""

    def test_accepts_simple_labels(self) -> None:
        validate_labels({"env": "prod", "tenant.id": "acme", "region-1": "Warszawa"})

    def test_accepts_printable_unicode_value(self) -> None:
        validate_labels({"city": "São Paulo"})

    def test_rejects_non_dict_container(self) -> None:
        with pytest.raises(TypeError, match=r"labels must be dict\[str, str\]"):
            validate_labels([("env", "prod")])  # type: ignore[arg-type]  # negative test

    def test_rejects_too_many_labels(self) -> None:
        labels = {f"k{i}": "v" for i in range(MAX_LABELS + 1)}
        with pytest.raises(ValueError, match=f"at most {MAX_LABELS}"):
            validate_labels(labels)

    def test_rejects_non_string_key(self) -> None:
        with pytest.raises(TypeError, match="Label key must be str"):
            validate_labels({1: "prod"})  # type: ignore[arg-type]  # negative test

    def test_rejects_empty_key(self) -> None:
        with pytest.raises(ValueError, match="must not be empty"):
            validate_labels({"": "prod"})

    def test_rejects_key_with_space(self) -> None:
        with pytest.raises(ValueError, match="invalid"):
            validate_labels({"bad key": "prod"})

    def test_rejects_key_with_control_character(self) -> None:
        with pytest.raises(ValueError, match="invalid"):
            validate_labels({"bad\nkey": "prod"})

    def test_rejects_key_with_trailing_newline(self) -> None:
        with pytest.raises(ValueError, match="invalid"):
            validate_labels({"team\n": "prod"})

    def test_rejects_key_with_non_ascii(self) -> None:
        with pytest.raises(ValueError, match="invalid"):
            validate_labels({"tęam": "prod"})

    def test_rejects_overlong_key(self) -> None:
        with pytest.raises(ValueError, match=f"max is {MAX_LABEL_KEY_LENGTH}"):
            validate_labels({"a" * (MAX_LABEL_KEY_LENGTH + 1): "prod"})

    def test_rejects_non_string_value(self) -> None:
        with pytest.raises(TypeError, match="must be str"):
            validate_labels({"env": 1})  # type: ignore[arg-type]  # negative test

    def test_rejects_value_with_c0_control_character(self) -> None:
        with pytest.raises(ValueError, match="control characters"):
            validate_labels({"env": "pro\nd"})

    def test_rejects_value_with_del_character(self) -> None:
        with pytest.raises(ValueError, match="control characters"):
            validate_labels({"env": "prod\x7f"})

    def test_rejects_value_with_c1_control_character(self) -> None:
        with pytest.raises(ValueError, match="control characters"):
            validate_labels({"env": "prod\x85"})

    def test_rejects_overlong_utf8_value(self) -> None:
        too_long = "é" * (MAX_LABEL_VALUE_BYTES // 2 + 1)
        with pytest.raises(ValueError, match=f"max is {MAX_LABEL_VALUE_BYTES}"):
            validate_labels({"env": too_long})
