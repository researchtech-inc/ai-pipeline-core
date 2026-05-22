"""StreamAccumulator provider metadata and citation passthrough tests."""

from types import SimpleNamespace

from ai_pipeline_core._llm_core._stream import StreamAccumulator


def _chunk(*, delta: object | None = None, message: object | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        id="chunk-1",
        choices=[SimpleNamespace(delta=delta, message=message, finish_reason=None)],
        usage=None,
    )


def test_merge_provider_fields_deep_merges_nested_dicts() -> None:
    accumulator = StreamAccumulator()

    accumulator._merge_provider_fields({"groundingMetadata": {"metadata": {"a": 1}}})
    accumulator._merge_provider_fields({"groundingMetadata": {"metadata": {"b": 2}}})

    assert accumulator.provider_specific_fields == {"groundingMetadata": {"metadata": {"a": 1, "b": 2}}}


def test_merge_provider_fields_extends_lists() -> None:
    accumulator = StreamAccumulator()

    accumulator._merge_provider_fields({
        "groundingMetadata": {"groundingChunks": [{"web": {"uri": "https://one.test"}}]}
    })
    accumulator._merge_provider_fields({
        "groundingMetadata": {"groundingChunks": [{"web": {"uri": "https://two.test"}}]}
    })

    assert accumulator.provider_specific_fields["groundingMetadata"]["groundingChunks"] == [
        {"web": {"uri": "https://one.test"}},
        {"web": {"uri": "https://two.test"}},
    ]


def test_add_chunk_collects_delta_and_message_annotations() -> None:
    accumulator = StreamAccumulator()
    delta = SimpleNamespace(role="assistant", content="", annotations=["delta-citation"])
    message = SimpleNamespace(annotations=["message-citation"])

    accumulator.add_chunk(_chunk(delta=delta, message=message))

    assert accumulator.annotations == ["delta-citation", "message-citation"]


def test_add_chunk_merges_final_message_provider_fields_without_delta() -> None:
    accumulator = StreamAccumulator()
    message = SimpleNamespace(
        provider_specific_fields={"groundingMetadata": {"groundingChunks": [{"web": {"uri": "https://final.test"}}]}}
    )

    accumulator.add_chunk(_chunk(delta=None, message=message))

    assert accumulator.provider_specific_fields == {
        "groundingMetadata": {"groundingChunks": [{"web": {"uri": "https://final.test"}}]}
    }


def test_streamed_and_final_provider_fields_converge() -> None:
    accumulator = StreamAccumulator()
    delta = SimpleNamespace(
        role="assistant",
        content="",
        provider_specific_fields={"groundingMetadata": {"groundingChunks": [{"web": {"uri": "https://delta.test"}}]}},
    )
    message = SimpleNamespace(
        provider_specific_fields={"groundingMetadata": {"groundingChunks": [{"web": {"uri": "https://message.test"}}]}}
    )

    accumulator.add_chunk(_chunk(delta=delta, message=message))

    assert accumulator.provider_specific_fields["groundingMetadata"]["groundingChunks"] == [
        {"web": {"uri": "https://delta.test"}},
        {"web": {"uri": "https://message.test"}},
    ]
