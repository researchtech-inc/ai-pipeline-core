"""Unit tests that pin vision preset conversion behavior before fallback."""

import inspect

from ai_pipeline_core._llm_core.types import ImageContent, ImagePreset
from ai_pipeline_core.llm import Conversation
from ai_pipeline_core.llm._request_assembly import to_core_messages
from ai_pipeline_core.llm._images import process_image
from tests.support.helpers import ConcreteDocument, make_text_image_tile
from tests.support.model_catalog import DEFAULT_TEST_MODEL, ALTERNATE_TEST_MODEL


def _image_part_count(content: object) -> int:
    """Return the number of image parts in one CoreMessage content payload."""
    if isinstance(content, ImageContent):
        return 1
    if isinstance(content, tuple):
        return sum(1 for part in content if isinstance(part, ImageContent))
    return 0


def test_to_core_messages_uses_passed_model_vision_preset() -> None:
    """Image conversion uses the AIModel passed to the converter, not its fallback."""
    image = make_text_image_tile(
        ["ALPHA", "BRAVO", "CHARLIE", "DELTA", "ECHO", "FOXTROT", "GOLF", "HOTEL"], width=4000, tile_height=500
    )
    doc = ConcreteDocument.create_root(name="fallback-vision.jpg", content=image, reason="phase3 vision preset")
    primary = DEFAULT_TEST_MODEL.model_copy(
        update={
            "vision_preset": ImagePreset.HIGH_RES,
            "fallback": ALTERNATE_TEST_MODEL.model_copy(update={"vision_preset": ImagePreset.COMPACT}),
        },
    )
    fallback = primary.fallback
    assert fallback is not None

    primary_messages = to_core_messages((doc,), primary)
    fallback_messages = to_core_messages((doc,), fallback)
    primary_count = _image_part_count(primary_messages[0].content)
    fallback_count = _image_part_count(fallback_messages[0].content)

    assert primary_count == len(process_image(image, preset=ImagePreset.HIGH_RES).parts)
    assert fallback_count == len(process_image(image, preset=ImagePreset.COMPACT).parts)
    assert primary_count != fallback_count


def test_to_core_messages_does_not_inspect_fallback_vision_preset() -> None:
    """Changing only the fallback preset must not change primary conversion."""
    image = make_text_image_tile(["INDIA", "JULIET", "KILO", "LIMA", "MIKE", "NOVEMBER"], width=4000, tile_height=500)
    doc = ConcreteDocument.create_root(
        name="primary-only-vision.jpg", content=image, reason="phase3 vision fallback ignored"
    )
    compact_fallback = ALTERNATE_TEST_MODEL.model_copy(update={"vision_preset": ImagePreset.COMPACT})
    high_res_fallback = ALTERNATE_TEST_MODEL.model_copy(update={"vision_preset": ImagePreset.HIGH_RES})
    primary_a = DEFAULT_TEST_MODEL.model_copy(
        update={"vision_preset": ImagePreset.BALANCED, "fallback": compact_fallback}
    )
    primary_b = DEFAULT_TEST_MODEL.model_copy(
        update={"vision_preset": ImagePreset.BALANCED, "fallback": high_res_fallback}
    )

    count_a = _image_part_count(to_core_messages((doc,), primary_a)[0].content)
    count_b = _image_part_count(to_core_messages((doc,), primary_b)[0].content)

    assert count_a == count_b


def test_conversation_execute_converts_context_with_primary_model() -> None:
    """Conversation._execute passes its primary AIModel to message assembly."""
    source = inspect.getsource(Conversation._execute)

    assert "context=self.context" in source
    assert "model=self.model" in source
    assert "self.model.fallback" not in source
