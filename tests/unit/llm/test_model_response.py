"""Tests for ModelResponse[T] unified response class."""

from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

from ai_pipeline_core._llm_core.model_response import Citation
from ai_pipeline_core._llm_core.model_response import ModelResponse
from ai_pipeline_core._llm_core.types import TokenUsage
from tests.support.helpers import create_test_model_response, create_test_structured_model_response


class TestModelResponse:
    """Test ModelResponse class."""

    def test_construct_unstructured_response(self):
        """Test constructing ModelResponse[str] for unstructured output."""
        response = create_test_model_response(
            content="Test response",
            model="test-model",
            response_id="test-id",
        )

        assert response.content == "Test response"
        assert response.parsed == "Test response"
        assert response.model == "test-model"
        assert response.response_id == "test-id"

    def test_content_property(self):
        """Test content property accessor."""
        response = create_test_model_response(content="Content here")
        assert response.content == "Content here"

    def test_parsed_equals_content_for_unstructured(self):
        """Test that parsed equals content for unstructured responses."""
        response = create_test_model_response(content="Some text")
        assert response.parsed == response.content

    def test_reasoning_content_empty_by_default(self):
        """Test reasoning_content is empty by default."""
        response = create_test_model_response(content="Simple response")
        assert response.reasoning_content == ""

    def test_reasoning_content_preserved(self):
        """Test reasoning_content is preserved when set."""
        response = create_test_model_response(
            content="Visible content",
            reasoning_content="Internal reasoning",
        )
        assert response.reasoning_content == "Internal reasoning"
        assert response.content == "Visible content"

    def test_usage_property(self):
        """Test usage property works directly."""
        response = create_test_model_response(
            content="test",
            prompt_tokens=10,
            completion_tokens=20,
        )

        assert response.usage.prompt_tokens == 10
        assert response.usage.completion_tokens == 20
        assert response.usage.total_tokens == 30

    def test_frozen_model(self):
        """Test that ModelResponse is immutable."""
        response = create_test_model_response(content="test")

        with pytest.raises(ValidationError):
            response.content = "new content"  # type: ignore[misc]  # frozen model mutation negative test


class TestModelResponseSerialization:
    """Test ModelResponse serialization."""

    def test_json_roundtrip_unstructured(self):
        """Test JSON serialization roundtrip for unstructured response."""
        response = create_test_model_response(
            content="Test content",
            reasoning_content="Some reasoning",
            model="gpt-5.4-mini",
            response_id="test-123",
            cost=0.01,
        )

        json_str = response.model_dump_json()
        restored = ModelResponse[Any].model_validate_json(json_str)

        assert restored.content == response.content
        assert restored.parsed == response.parsed
        assert restored.reasoning_content == response.reasoning_content
        assert restored.model == response.model
        assert restored.response_id == response.response_id
        assert restored.cost == response.cost

    def test_json_roundtrip_structured(self):
        """Test JSON serialization roundtrip for structured response."""

        class MyModel(BaseModel):
            name: str
            value: int

        parsed = MyModel(name="test", value=42)
        response = create_test_structured_model_response(parsed=parsed)

        json_str = response.model_dump_json()
        restored = ModelResponse[Any].model_validate_json(json_str)

        # After deserialization, parsed is a dict
        assert isinstance(restored.parsed, dict)
        assert restored.parsed["name"] == "test"
        assert restored.parsed["value"] == 42

        # Can reconstruct typed model
        typed = MyModel.model_validate(restored.parsed)
        assert typed.name == "test"
        assert typed.value == 42


class TestCitations:
    """Test citations handling."""

    def test_citations_empty_by_default(self):
        """Test citations returns empty tuple by default."""
        response = create_test_model_response(content="No citations")
        assert response.citations == ()

    def test_citations_preserved(self):
        """Test citations are preserved when set."""
        citations = (
            Citation(title="Page 1", url="https://example.com", start_index=0, end_index=10),
            Citation(title="Page 2", url="https://other.com", start_index=20, end_index=30),
        )
        response = ModelResponse[str](
            content="Test content",
            parsed="Test content",
            reasoning_content="",
            citations=citations,
            usage=TokenUsage(prompt_tokens=10, completion_tokens=20, total_tokens=30),
            cost=None,
            model="test",
            response_id="test-id",
        )

        assert len(response.citations) == 2
        assert response.citations[0].title == "Page 1"
        assert response.citations[1].url == "https://other.com"

    def test_citations_serialization(self):
        """Test citations serialize correctly."""
        citations = (Citation(title="Test", url="https://test.com", start_index=0, end_index=5),)
        response = ModelResponse[str](
            content="Test",
            parsed="Test",
            citations=citations,
            usage=TokenUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
            model="test",
            response_id="id",
        )

        json_str = response.model_dump_json()
        restored = ModelResponse.model_validate_json(json_str)

        # Citations are serialized as dicts
        assert len(restored.citations) == 1
        # After deserialization, citations are dicts (not Citation dataclass)
        citation = restored.citations[0]
        if isinstance(citation, dict):
            assert citation["title"] == "Test"
            assert citation["url"] == "https://test.com"
        else:
            assert citation.title == "Test"
            assert citation.url == "https://test.com"


class TestStructuredModelResponse:
    """Test ModelResponse with structured output."""

    class ExampleModel(BaseModel):
        """Example Pydantic model for testing."""

        field1: str
        field2: int

    def test_parsed_property_typed(self):
        """Test that parsed returns typed model."""
        parsed = self.ExampleModel(field1="test", field2=42)
        response = create_test_structured_model_response(parsed=parsed)

        assert response.parsed.field1 == "test"
        assert response.parsed.field2 == 42

    def test_content_contains_json(self):
        """Test that content contains JSON string."""
        parsed = self.ExampleModel(field1="value", field2=123)
        response = create_test_structured_model_response(parsed=parsed)

        import json

        content_dict = json.loads(response.content)
        assert content_dict["field1"] == "value"
        assert content_dict["field2"] == 123

    def test_model_response_properties_available(self):
        """Test that ModelResponse properties are available."""
        parsed = self.ExampleModel(field1="x", field2=1)
        response = create_test_structured_model_response(
            parsed=parsed,
            model="gpt-5.4-mini",
            response_id="test-id",
            cost=0.02,
        )

        assert response.model == "gpt-5.4-mini"
        assert response.response_id == "test-id"
        assert response.cost == pytest.approx(0.02)
