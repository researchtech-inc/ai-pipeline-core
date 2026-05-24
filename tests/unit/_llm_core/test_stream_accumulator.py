"""StreamAccumulator provider metadata and citation passthrough tests."""

import json
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


def _tool_call_delta(
    *, index: int = 0, call_id: str | None = None, name: str | None = None, arguments: str = ""
) -> SimpleNamespace:
    function = (
        SimpleNamespace(name=name, arguments=arguments)
        if (name or arguments)
        else SimpleNamespace(name=None, arguments=arguments)
    )
    return SimpleNamespace(
        role=None,
        content=None,
        tool_calls=[SimpleNamespace(index=index, id=call_id, function=function, provider_specific_fields=None)],
    )


def test_tool_call_delta_bumps_both_content_and_tool_call_counters() -> None:
    """One tool_call delta increments BOTH last_content_tokens and last_tool_call_tokens by 1.

    The watchdog uses ``last_content_tokens - last_tool_call_tokens`` to recover
    the text-token count for the chunk, so the invariant ``tool_call <= content``
    is structural.
    """
    accumulator = StreamAccumulator()
    accumulator.add_chunk(
        SimpleNamespace(
            id="chunk-tool",
            choices=[
                SimpleNamespace(
                    delta=_tool_call_delta(call_id="ws_1", name="web_search", arguments='{"q":"x"}'),
                    message=None,
                    finish_reason=None,
                )
            ],
            usage=None,
        )
    )

    assert accumulator.last_content_tokens == 1
    assert accumulator.last_tool_call_tokens == 1


def test_empty_arguments_tool_call_delta_does_not_corrupt_accumulated_arguments() -> None:
    """Tool-call keepalive deltas with ``arguments=""`` must bump the liveness
    counters without appending to ``slot.arguments``. Otherwise the empty
    string would invalidate the JSON already accumulated from the initial
    action-payload delta.
    """
    accumulator = StreamAccumulator()

    # Initial chunk with the action payload.
    accumulator.add_chunk(
        SimpleNamespace(
            id="initial",
            choices=[
                SimpleNamespace(
                    delta=_tool_call_delta(call_id="ws_1", name="web_search", arguments='{"q":"foo"}'),
                    message=None,
                    finish_reason=None,
                )
            ],
            usage=None,
        )
    )

    # Lifecycle keepalive chunks with empty arguments.
    for _ in range(5):
        accumulator.add_chunk(
            SimpleNamespace(
                id="keepalive",
                choices=[
                    SimpleNamespace(
                        delta=_tool_call_delta(call_id="ws_1", arguments=""), message=None, finish_reason=None
                    )
                ],
                usage=None,
            )
        )
        assert accumulator.last_tool_call_tokens == 1
        assert accumulator.last_content_tokens == 1

    slot = accumulator.tool_calls[0]
    assert slot.id == "ws_1"
    assert slot.name == "web_search"
    assert json.loads(slot.arguments) == {"q": "foo"}


def test_mixed_chunk_separates_text_and_tool_call_token_counts() -> None:
    """A chunk that carries both visible text AND a tool_call delta produces
    ``last_tool_call_tokens=1`` and ``last_content_tokens=1 + estimate(text)``.
    """
    accumulator = StreamAccumulator()
    delta = SimpleNamespace(
        role=None,
        content="hello world!",
        tool_calls=[
            SimpleNamespace(
                index=0, id="t1", function=SimpleNamespace(name="f", arguments=""), provider_specific_fields=None
            )
        ],
    )
    accumulator.add_chunk(
        SimpleNamespace(
            id="mixed", choices=[SimpleNamespace(delta=delta, message=None, finish_reason=None)], usage=None
        )
    )

    assert accumulator.last_tool_call_tokens == 1
    assert accumulator.last_content_tokens > accumulator.last_tool_call_tokens


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
