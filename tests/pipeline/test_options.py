"""Tests for FlowOptions inheritance and PipelineFlow compatibility."""

from typing import Any

import pytest
from pydantic import BaseModel, Field, ValidationError, model_validator

from ai_pipeline_core import Document, FlowOptions
from ai_pipeline_core.pipeline import PipelineFlow


class InDoc(Document):
    pass


class OutDoc(Document):
    pass


class TestFlowOptionsInheritance:
    """Test FlowOptions can be inherited and extended."""

    def test_base_flow_options_is_empty(self):
        """Test that base FlowOptions has no predefined fields."""
        options = FlowOptions()
        assert not hasattr(options, "core_model")
        assert not hasattr(options, "small_model")

    def test_base_flow_options_rejects_extra(self):
        """Test that base FlowOptions rejects extra fields (extra='forbid')."""
        with pytest.raises(ValidationError, match="extra_forbidden"):
            FlowOptions(unknown_field="value")

    def test_flow_options_is_frozen(self):
        """Test that FlowOptions instances are immutable."""

        class SimpleOptions(FlowOptions):
            core_model: str = "default"

        options = SimpleOptions()
        with pytest.raises(ValidationError):
            options.core_model = "new-model"

    def test_inherited_flow_options_basic(self):
        """Test basic inheritance from FlowOptions."""

        class ProjectFlowOptions(FlowOptions):
            """Project-specific flow options."""

            core_model: str = "gemini-3.1-pro"
            small_model: str = "grok-4.1-fast"
            batch_max_chars: int = Field(default=100_000, gt=0)
            batch_max_files: int = Field(default=25, gt=0)
            enable_caching: bool = Field(default=True)

        # Test with defaults
        options = ProjectFlowOptions()
        assert options.core_model == "gemini-3.1-pro"
        assert options.small_model == "grok-4.1-fast"
        assert options.batch_max_chars == 100_000
        assert options.batch_max_files == 25
        assert options.enable_caching is True

        # Test with custom values
        options = ProjectFlowOptions(core_model="custom-model", batch_max_chars=200_000, enable_caching=False)
        assert options.core_model == "custom-model"
        assert options.batch_max_chars == 200_000
        assert options.enable_caching is False

    def test_inherited_flow_options_with_lists(self):
        """Test inheritance with list fields."""

        class ExtendedFlowOptions(FlowOptions):
            """Extended options with model lists."""

            supporting_models: list[str] = Field(default_factory=list)
            search_models: list[str] = Field(default_factory=lambda: ["sonar-pro-search"])
            tags: list[str] = Field(default_factory=list)

        options = ExtendedFlowOptions(supporting_models=["model1", "model2"], tags=["tag1", "tag2"])
        assert options.supporting_models == ["model1", "model2"]
        assert options.search_models == ["sonar-pro-search"]
        assert options.tags == ["tag1", "tag2"]

    def test_inherited_flow_options_with_nested_models(self):
        """Test inheritance with nested Pydantic models."""

        class DatabaseConfig(BaseModel):
            host: str = "localhost"
            port: int = 5432
            database: str = "ai_pipeline"

        class AdvancedFlowOptions(FlowOptions):
            """Options with nested configuration."""

            database: DatabaseConfig = Field(default_factory=DatabaseConfig)
            max_retries: int = Field(default=3, ge=0)

        options = AdvancedFlowOptions()
        assert options.database.host == "localhost"
        assert options.database.port == 5432
        assert options.max_retries == 3

        # Test with custom database config
        custom_db = DatabaseConfig(host="remote", port=3306, database="custom")
        options = AdvancedFlowOptions(database=custom_db, max_retries=5)
        assert options.database.host == "remote"
        assert options.database.port == 3306
        assert options.max_retries == 5

    def test_inherited_flow_options_maintains_frozen(self):
        """Test that inherited classes maintain frozen configuration."""

        class CustomFlowOptions(FlowOptions):
            custom_field: str = "default"

        options = CustomFlowOptions()
        with pytest.raises(ValidationError):
            options.custom_field = "new_value"

    def test_inherited_flow_options_with_validators(self):
        """Test inheritance with custom validators."""

        class ValidatedFlowOptions(FlowOptions):
            """Options with custom validation."""

            core_model: str = "gemini-3.1-pro"
            small_model: str = "grok-4.1-fast"
            temperature: float = Field(default=0.7, ge=0.0, le=2.0)

            @model_validator(mode="after")
            def validate_temperature_model_combination(self) -> ValidatedFlowOptions:
                if self.temperature > 1.5 and self.core_model == self.small_model:
                    raise ValueError("High temperature requires different core and small models")
                return self

        # Valid options
        options = ValidatedFlowOptions(temperature=0.5)
        assert options.temperature == pytest.approx(0.5)

        # Valid high temperature with different models
        options = ValidatedFlowOptions(temperature=1.8, core_model="gpt-5.4", small_model="gpt-5.4-mini")
        assert options.temperature == pytest.approx(1.8)

        # Invalid temperature
        with pytest.raises(ValidationError):
            ValidatedFlowOptions(temperature=2.5)


# ---------------------------------------------------------------------------
# PipelineFlow with FlowOptions subclasses
# ---------------------------------------------------------------------------


class ChildOptions(FlowOptions):
    mode: str = "default"


def test_pipeline_flow_accepts_flowoptions_subclass() -> None:
    class MyFlow(PipelineFlow):
        async def run(self, input_docs: tuple[InDoc, ...], options: ChildOptions) -> tuple[OutDoc, ...]:
            _ = (input_docs, options)
            return ()

    assert MyFlow.input_document_types == (InDoc,)
    assert MyFlow.output_document_types == (OutDoc,)


class TestPipelineFlowWithInheritedOptions:
    """Test that PipelineFlow works with inherited FlowOptions."""

    def test_with_base_options(self):
        """Test PipelineFlow with base FlowOptions."""

        class BaseOptionsFlow(PipelineFlow):
            async def run(self, input_docs: tuple[InDoc, ...], options: FlowOptions) -> tuple[OutDoc, ...]:
                _ = input_docs
                assert isinstance(options, FlowOptions)
                return ()

        assert BaseOptionsFlow.input_document_types == (InDoc,)
        assert BaseOptionsFlow.output_document_types == (OutDoc,)

    def test_with_custom_options(self):
        """Test PipelineFlow with custom FlowOptions subclass."""

        class CustomFlowOptions(FlowOptions):
            core_model: str = "gemini-3.1-pro"
            batch_size: int = Field(default=10, gt=0)
            enable_logging: bool = Field(default=True)

        class CustomOptionsFlow(PipelineFlow):
            async def run(self, input_docs: tuple[InDoc, ...], options: CustomFlowOptions) -> tuple[OutDoc, ...]:
                _ = input_docs
                assert isinstance(options, CustomFlowOptions)
                assert isinstance(options, FlowOptions)
                return ()

        assert CustomOptionsFlow.input_document_types == (InDoc,)
        assert CustomOptionsFlow.output_document_types == (OutDoc,)

    def test_with_complex_inherited_options(self):
        """Test PipelineFlow with complex inherited options including nested models."""

        class APIConfig(BaseModel):
            endpoint: str = "https://api.example.com"
            timeout: int = 30
            retry_count: int = 3

        class AdvancedFlowOptions(FlowOptions):
            core_model: str = "gemini-3.1-pro"
            small_model: str = "grok-4.1-fast"
            api_config: APIConfig = Field(default_factory=APIConfig)
            processing_modes: list[str] = Field(default_factory=lambda: ["fast", "accurate"])
            metadata: dict[str, Any] = Field(default_factory=dict)

        class AdvancedFlow(PipelineFlow):
            async def run(self, input_docs: tuple[InDoc, ...], options: AdvancedFlowOptions) -> tuple[OutDoc, ...]:
                _ = (input_docs, options)
                return ()

        assert AdvancedFlow.input_document_types == (InDoc,)
        assert AdvancedFlow.output_document_types == (OutDoc,)

    def test_type_checking_required_field(self):
        """Test PipelineFlow with FlowOptions that has required fields."""

        class StrictFlowOptions(FlowOptions):
            required_field: str  # No default - required field

        class StrictFlow(PipelineFlow):
            async def run(self, input_docs: tuple[InDoc, ...], options: StrictFlowOptions) -> tuple[OutDoc, ...]:
                _ = input_docs
                assert options.required_field == "test-value"
                return ()

        assert StrictFlow.input_document_types == (InDoc,)

        with pytest.raises(ValidationError):
            StrictFlowOptions()  # type: ignore[call-arg]

    def test_multiple_inheritance_levels(self):
        """Test FlowOptions with multiple inheritance levels."""

        class BaseProjectOptions(FlowOptions):
            core_model: str = "gemini-3.1-pro"
            organization: str = "default-org"
            environment: str = "development"

        class SpecificProjectOptions(BaseProjectOptions):
            feature_flags: dict[str, bool] = Field(default_factory=dict)

        class MultiLevelFlow(PipelineFlow):
            async def run(self, input_docs: tuple[InDoc, ...], options: SpecificProjectOptions) -> tuple[OutDoc, ...]:
                _ = (input_docs, options)
                return ()

        assert MultiLevelFlow.input_document_types == (InDoc,)
        assert MultiLevelFlow.output_document_types == (OutDoc,)

    def test_with_pydantic_field_definitions(self):
        """Test FlowOptions with Pydantic Field definitions."""
        PRIMARY_MODELS = ["gpt-5.4", "gpt-5.4-mini"]
        SMALL_MODELS = ["gpt-5.4-mini", "gemini-3-flash"]
        SEARCH_MODELS = ["sonar", "gemini-3-flash-search"]

        class ProjectFlowOptions(FlowOptions):
            primary_models: list[str] = Field(default_factory=lambda: PRIMARY_MODELS.copy())
            small_models_list: list[str] = Field(default_factory=lambda: SMALL_MODELS.copy())
            search_models: list[str] = Field(default_factory=lambda: SEARCH_MODELS.copy())

        class FieldFlow(PipelineFlow):
            async def run(self, input_docs: tuple[InDoc, ...], options: ProjectFlowOptions) -> tuple[OutDoc, ...]:
                _ = (input_docs, options)
                return ()

        assert FieldFlow.input_document_types == (InDoc,)
        assert FieldFlow.output_document_types == (OutDoc,)
