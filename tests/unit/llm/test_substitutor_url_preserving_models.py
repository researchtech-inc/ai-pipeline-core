"""Tests for auto-disabling URLSubstitutor on URL-preserving models."""

from typing import Any

import pytest

from ai_pipeline_core.llm import AIModel
from ai_pipeline_core.llm.conversation import Conversation
from tests.support.model_catalog import DEFAULT_TEST_MODEL


class TestSubstitutorUrlPreservingModels:
    """Verify Conversation consumes model URL-preservation capability."""

    def test_conversation_auto_disables_substitutor_for_preserve_input_urls_model(self, default_test_model: AIModel):
        """Conversation consumes the resolved AIModel capability."""
        conv = Conversation(model=default_test_model.model_copy(update={"preserve_input_urls": True}))
        assert conv.enable_substitutor is False

    def test_explicit_enable_overrides_auto_disable(self, default_test_model: AIModel):
        """Explicitly enabling substitutor on a URL-preserving model should be respected."""
        conv = Conversation(
            model=default_test_model.model_copy(update={"preserve_input_urls": True}), enable_substitutor=True
        )
        assert conv.enable_substitutor is True

    def test_explicit_disable_on_default_model(self):
        """Explicitly disabling substitutor on the default model should work."""
        conv = Conversation(model=DEFAULT_TEST_MODEL, enable_substitutor=False)
        assert conv.enable_substitutor is False

    def test_conversation_rejects_string_model(self):
        """Conversation only accepts resolved AIModel instances."""
        model: Any = "any-string"

        with pytest.raises(TypeError, match=r"Conversation\.model must be an AIModel"):
            Conversation(model=model)

    def test_with_model_rejects_string_model(self):
        """with_model also keeps the Conversation boundary AIModel-only."""
        conv = Conversation(model=DEFAULT_TEST_MODEL)
        model: Any = "not-a-model-name"

        with pytest.raises(TypeError, match=r"Conversation\.with_model"):
            conv.with_model(model)
