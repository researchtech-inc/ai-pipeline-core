"""Comprehensive tests for ModelResponse[T] features."""

import json
from typing import Any

import pytest

from pydantic import BaseModel

from ai_pipeline_core._llm_core._transport_metadata import TransportMetadata, TransportTiming
from ai_pipeline_core._llm_core.model_response import ModelResponse
from ai_pipeline_core._llm_core.types import TokenUsage
from tests.support.helpers import create_test_model_response, create_test_structured_model_response


class TestModelResponseTransportMetadata:
    """Test typed transport metadata in ModelResponse."""

    def test_transport_metadata_stored_in_response(self):
        """Test that transport metadata is stored in the response."""
        response = ModelResponse[str](
            content="test",
            parsed="test",
            usage=TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
            model="test-model",
            response_id="test-id",
            transport=TransportMetadata(timing=TransportTiming(time_taken_s=1.5, first_token_s=0.3)),
        )

        assert response.transport.timing.time_taken_s == pytest.approx(1.5)
        assert response.transport.timing.first_token_s == pytest.approx(0.3)

    def test_metadata_includes_usage(self):
        """Test that metadata includes usage information."""
        response = create_test_model_response(
            content="test",
            prompt_tokens=100,
            completion_tokens=50,
        )

        assert response.usage.prompt_tokens == 100
        assert response.usage.completion_tokens == 50
        assert response.usage.total_tokens == 150


class TestModelResponseReasoningContent:
    """Test reasoning_content property."""

    def test_no_reasoning_content(self):
        """Test when there is no reasoning content."""
        response = create_test_model_response(content="Simple response")
        assert response.reasoning_content == ""
        assert response.content == "Simple response"

    def test_reasoning_content_preserved(self):
        """Test reasoning content is preserved when set."""
        response = create_test_model_response(
            content="Final answer",
            reasoning_content="My internal reasoning here",
        )
        assert response.reasoning_content == "My internal reasoning here"
        assert response.content == "Final answer"

    def test_reasoning_content_with_multiline(self):
        """Test reasoning with multiline content."""
        reasoning = """Step 1: Analyze the problem
Step 2: Consider options
Step 3: Choose best solution"""
        response = create_test_model_response(
            content="Here is the final answer",
            reasoning_content=reasoning,
        )
        assert "Step 1" in response.reasoning_content
        assert "Step 3" in response.reasoning_content
        assert response.content == "Here is the final answer"


class TestModelResponseUsage:
    """Test usage tracking."""

    def test_usage_basic(self):
        """Test basic usage tracking."""
        response = create_test_model_response(
            content="test",
            prompt_tokens=100,
            completion_tokens=50,
        )

        assert response.usage.prompt_tokens == 100
        assert response.usage.completion_tokens == 50
        assert response.usage.total_tokens == 150

    def test_usage_with_cached_tokens(self):
        """Test usage with cached tokens."""
        response = ModelResponse[str](
            content="test",
            parsed="test",
            usage=TokenUsage(
                prompt_tokens=100,
                completion_tokens=50,
                total_tokens=150,
                cached_tokens=80,
            ),
            model="test",
            response_id="test-id",
        )

        assert response.usage.cached_tokens == 80

    def test_usage_with_reasoning_tokens(self):
        """Test usage with reasoning tokens."""
        response = ModelResponse[str](
            content="test",
            parsed="test",
            usage=TokenUsage(
                prompt_tokens=100,
                completion_tokens=50,
                total_tokens=150,
                reasoning_tokens=30,
            ),
            model="test",
            response_id="test-id",
        )

        assert response.usage.reasoning_tokens == 30


class TestStructuredModelResponse:
    """Test ModelResponse with structured output."""

    class SampleModel(BaseModel):
        field: str
        value: int

    def test_structured_response_creation(self):
        """Test creating a structured response."""
        parsed = self.SampleModel(field="test", value=42)
        response = create_test_structured_model_response(parsed=parsed)

        assert response.parsed.field == "test"
        assert response.parsed.value == 42

    def test_structured_response_content_is_json(self):
        """Test that content is JSON string."""
        parsed = self.SampleModel(field="test", value=42)
        response = create_test_structured_model_response(parsed=parsed)

        # Content should be valid JSON
        content_dict = json.loads(response.content)
        assert content_dict["field"] == "test"
        assert content_dict["value"] == 42

    def test_structured_with_complex_model(self):
        """Test with complex nested Pydantic model."""

        class NestedModel(BaseModel):
            inner: str

        class ComplexModel(BaseModel):
            name: str
            count: int
            nested: NestedModel
            items: list[str]

        parsed = ComplexModel(
            name="test",
            count=5,
            nested=NestedModel(inner="value"),
            items=["a", "b", "c"],
        )
        response = create_test_structured_model_response(parsed=parsed)

        assert response.parsed.name == "test"
        assert response.parsed.count == 5
        assert response.parsed.nested.inner == "value"
        assert response.parsed.items == ["a", "b", "c"]


class TestModelResponseSerialization:
    """Test serialization features."""

    def test_serialization_roundtrip(self):
        """Test JSON serialization roundtrip."""
        response = create_test_model_response(
            content="test content",
            reasoning_content="some reasoning",
            cost=0.05,
        )

        json_str = response.model_dump_json()
        restored = ModelResponse[Any].model_validate_json(json_str)

        assert restored.content == response.content
        assert restored.reasoning_content == response.reasoning_content
        assert restored.cost == response.cost

    def test_structured_serialization_roundtrip(self):
        """Test structured response serialization."""

        class MyModel(BaseModel):
            name: str
            value: int

        parsed = MyModel(name="test", value=123)
        response = create_test_structured_model_response(parsed=parsed)

        json_str = response.model_dump_json()
        restored = ModelResponse[Any].model_validate_json(json_str)

        # After deserialization, parsed is a dict
        assert isinstance(restored.parsed, dict)
        assert restored.parsed["name"] == "test"
        assert restored.parsed["value"] == 123

        # Can reconstruct typed model
        typed = MyModel.model_validate(restored.parsed)
        assert typed.name == "test"
        assert typed.value == 123

    def test_transport_json_serialization(self):
        """Test that transport metadata is JSON serializable."""
        response = create_test_model_response(content="test")
        json_str = json.dumps(response.transport, default=str)
        assert json_str is not None
        assert len(json_str) > 0
