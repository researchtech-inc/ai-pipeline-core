"""Tests for Conversation.with_model()."""

from ai_pipeline_core.llm import Conversation, ModelOptions
from tests.support.model_catalog import DEFAULT_TEST_MODEL, ALTERNATE_TEST_MODEL


class TestWithModel:
    """Test Conversation.with_model() method."""

    def test_with_model_changes_model(self):
        """with_model() returns a new Conversation with the specified model."""
        conv = Conversation(model=DEFAULT_TEST_MODEL)
        new_conv = conv.with_model(ALTERNATE_TEST_MODEL)

        assert new_conv.model == ALTERNATE_TEST_MODEL
        assert conv.model == DEFAULT_TEST_MODEL  # original unchanged

    def test_with_model_preserves_options(self):
        """with_model() preserves model_options from the original."""
        opts = ModelOptions(system_prompt="test", reasoning_effort="high")
        conv = Conversation(model=DEFAULT_TEST_MODEL, model_options=opts)
        new_conv = conv.with_model(ALTERNATE_TEST_MODEL)

        assert new_conv.model_options is opts
        assert new_conv.model_options.system_prompt == "test"

    def test_with_model_preserves_substitutor_setting(self):
        """with_model() preserves enable_substitutor flag."""
        conv = Conversation(model=DEFAULT_TEST_MODEL, enable_substitutor=False)
        new_conv = conv.with_model(ALTERNATE_TEST_MODEL)

        assert new_conv.enable_substitutor is False
