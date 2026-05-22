"""Prove call_spec() convenience function does not exist.

Single-shot spec calls require constructing a Conversation, calling send_spec,
and extracting the result. A call_spec() would reduce this to one call.
"""

import pytest

import ai_pipeline_core
import ai_pipeline_core.llm.conversation as conv_mod


class TestCallSpecDoesNotExist:
    """Prove: no call_spec convenience function exists."""

    def test_no_call_spec_in_conversation_module(self) -> None:
        """Passes on current code — no call_spec function."""
        assert not hasattr(conv_mod, "call_spec")

    def test_no_call_spec_at_top_level(self) -> None:
        """Passes on current code — not in the top-level package."""
        assert not hasattr(ai_pipeline_core, "call_spec")


class TestCallSpecNotImplemented:
    """Verify call_spec after implementation."""

    @pytest.mark.xfail(reason="call_spec() not yet implemented", strict=True)
    def test_call_spec_importable(self) -> None:
        """After fix: call_spec should be importable."""
        from ai_pipeline_core import call_spec  # type: ignore[attr-defined]

        assert callable(call_spec)
