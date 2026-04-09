"""Generic replay execution for recorded spans."""

import json
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from ai_pipeline_core._codec import UniversalCodec
from ai_pipeline_core._llm_core import ModelOptions
from ai_pipeline_core.database._protocol import DatabaseReader, DatabaseWriter
from ai_pipeline_core.deployment._types import _NoopPublisher
from ai_pipeline_core.llm.conversation import _LLM_ROUND_REPLAY_TARGET, Conversation
from ai_pipeline_core.llm.tools import Tool
from ai_pipeline_core.pipeline._execution_context import ReplayExecutionContext, set_execution_context
from ai_pipeline_core.pipeline._runtime_sinks import build_runtime_sinks
from ai_pipeline_core.settings import settings

from ._adapters import _invoke_callable, resolve_callable

__all__ = ["execute_span"]

_MISSING = object()


@dataclass(frozen=True, slots=True)
class _ExecutionOutcome:
    result: Any
    context: ReplayExecutionContext


def _resolve_replay_target(kind: str, target: str, *, span_id: UUID) -> str:
    if target:
        return target
    if kind == "llm_round":
        return _LLM_ROUND_REPLAY_TARGET
    raise ValueError(f"Span {span_id} has an empty target and is not replayable.")


def _parse_json_field(payload_json: str, *, field_name: str, span_id: UUID) -> Any:
    try:
        return json.loads(payload_json)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Span {span_id} has invalid {field_name}. Store valid JSON in spans.{field_name} before replaying.") from exc


def _merge_model_options(base: ModelOptions | None, overrides: dict[str, Any] | None) -> ModelOptions | None:
    if not overrides:
        return base
    base_payload = base.model_dump(mode="python", exclude_none=False) if base is not None else {}
    base_payload.update(overrides)
    return ModelOptions.model_validate(base_payload)


def _override_tools_in_recorded_order(value: Any, override_tools: dict[str, Tool]) -> list[Tool]:
    if not isinstance(value, (list, tuple)):
        return list(override_tools.values())
    ordered: list[Tool] = []
    matched_names: set[str] = set()
    for item in value:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        if isinstance(name, str) and name in override_tools:
            ordered.append(override_tools[name])
            matched_names.add(name)
    if not ordered:
        return list(override_tools.values())
    for name, tool in override_tools.items():
        if name not in matched_names:
            ordered.append(tool)
    return ordered


def _override_receiver(receiver: Any, overrides: Any) -> Any:
    if not isinstance(receiver, dict):
        return receiver
    mode = receiver.get("mode")
    value = receiver.get("value")
    if mode == "decoded_state" and isinstance(value, Conversation):
        updated = value
        if getattr(overrides, "model", None):
            updated = updated.model_copy(update={"model": overrides.model})
        merged = _merge_model_options(updated.model_options, getattr(overrides, "model_options", None))
        if merged != updated.model_options:
            updated = updated.model_copy(update={"model_options": merged})
        return {"mode": "decoded_state", "value": updated}
    if mode == "constructor_args" and isinstance(value, dict):
        updated_value = dict(value)
        if getattr(overrides, "model", None) is not None and "model" in updated_value:
            updated_value["model"] = overrides.model
        if "model_options" in updated_value:
            updated_value["model_options"] = _merge_model_options(updated_value.get("model_options"), getattr(overrides, "model_options", None))
        return {"mode": "constructor_args", "value": updated_value}
    return receiver


def _override_arguments(arguments: Any, overrides: Any) -> Any:
    if not isinstance(arguments, dict):
        return arguments
    result = dict(arguments)
    if "tools" in result:
        override_tools = getattr(overrides, "tools", None)
        if override_tools is not None:
            result["tools"] = _override_tools_in_recorded_order(result["tools"], dict(override_tools))
        elif result["tools"]:
            raise TypeError(
                "Recorded conversation used tools but no override_tools were provided. "
                "Pass override_tools= with live tool instances to replay tool-using conversations."
            )
    if getattr(overrides, "response_format", None) is not None:
        result["response_format"] = overrides.response_format
    if getattr(overrides, "model", None) is not None:
        result["model"] = overrides.model
    if "model_options" in result:
        result["model_options"] = _merge_model_options(result.get("model_options"), getattr(overrides, "model_options", None))
    return result


def _apply_overrides(
    *,
    receiver: Any,
    arguments: Any,
    overrides: Any | None,
) -> tuple[Any, Any]:
    if overrides is not None:
        receiver = _override_receiver(receiver, overrides)
        arguments = _override_arguments(arguments, overrides)
    elif isinstance(arguments, dict) and "tools" in arguments and arguments["tools"]:
        raise TypeError(
            "Recorded conversation used tools but no override_tools were provided. "
            "Pass override_tools= with live tool instances to replay tool-using conversations."
        )
    return receiver, arguments


async def _copy_blob(blob_sha: str, *, source_db: DatabaseReader, sink_db: DatabaseWriter) -> None:
    blob = await source_db.get_blob(blob_sha)
    if blob is None:
        raise FileNotFoundError(f"Replay could not copy blob {blob_sha[:12]}... into the sink database because it is missing from the source database.")
    await sink_db.save_blob(blob)


async def _copy_document(document_sha: str, *, source_db: DatabaseReader, sink_db: DatabaseWriter) -> None:
    document = await source_db.get_document(document_sha)
    if document is None:
        raise FileNotFoundError(f"Replay could not copy document {document_sha[:12]}... into the sink database because it is missing from the source database.")
    hydrated = await source_db.get_document_with_content(document_sha)
    if hydrated is None:
        raise FileNotFoundError(
            f"Replay could not hydrate document {document_sha[:12]}... from the source database. "
            "Persist the document record and all referenced blobs before replaying."
        )
    await sink_db.save_document(document)
    from ai_pipeline_core.database._types import _BlobRecord

    blobs = [_BlobRecord(content_sha256=hydrated.record.content_sha256, content=hydrated.content)]
    for att_sha, att_content in hydrated.attachment_contents.items():
        blobs.append(_BlobRecord(content_sha256=att_sha, content=att_content))
    await sink_db.save_blob_batch(blobs)


async def _copy_input_artifacts(
    *,
    input_document_shas: tuple[str, ...],
    input_blob_shas: tuple[str, ...],
    source_db: DatabaseReader,
    sink_db: DatabaseWriter | None,
) -> None:
    if sink_db is None or sink_db is source_db:
        return
    for blob_sha in input_blob_shas:
        await _copy_blob(blob_sha, source_db=source_db, sink_db=sink_db)
    for document_sha in input_document_shas:
        await _copy_document(document_sha, source_db=source_db, sink_db=sink_db)


async def _execute_span_internal(
    span_id: UUID,
    *,
    source_db: DatabaseReader,
    sink_db: DatabaseWriter | None,
    overrides: Any | None = None,
) -> _ExecutionOutcome:
    span = await source_db.get_span(span_id)
    if span is None:
        raise FileNotFoundError(f"Span {span_id} was not found in the source database.")
    replay_target = _resolve_replay_target(span.kind, span.target, span_id=span.span_id)

    codec = UniversalCodec()
    raw_receiver = _parse_json_field(span.receiver_json, field_name="receiver_json", span_id=span.span_id) if span.receiver_json else None
    raw_input = _parse_json_field(span.input_json, field_name="input_json", span_id=span.span_id) if span.input_json else _MISSING

    await _copy_input_artifacts(
        input_document_shas=span.input_document_shas,
        input_blob_shas=span.input_blob_shas,
        source_db=source_db,
        sink_db=sink_db,
    )

    decoded_receiver = None
    if raw_receiver is not None:
        if not isinstance(raw_receiver, dict):
            raise TypeError(f"Span {span.span_id} receiver_json must decode to a JSON object with mode/value fields.")
        decoded_receiver = {
            "mode": raw_receiver.get("mode"),
            "value": await codec.decode_async(raw_receiver.get("value"), db=source_db),
        }
    decoded_input = _MISSING
    if raw_input is not _MISSING:
        decoded_input = await codec.decode_async(raw_input, db=source_db)

    decoded_receiver, decoded_input = _apply_overrides(
        receiver=decoded_receiver,
        arguments=decoded_input,
        overrides=overrides,
    )

    replay_context = ReplayExecutionContext.create(
        source_span_id=span.span_id,
        database=sink_db,
        publisher=_NoopPublisher(),
        sinks=build_runtime_sinks(database=sink_db, settings_obj=settings).span_sinks,
    )

    callable_obj = resolve_callable(replay_target, decoded_receiver)
    with set_execution_context(replay_context):
        result = await _invoke_callable(callable_obj, decoded_input)
    return _ExecutionOutcome(result=result, context=replay_context)


async def execute_span(
    span_id: UUID,
    *,
    source_db: DatabaseReader,
    sink_db: DatabaseWriter | None = None,
) -> Any:
    """Replay one recorded span against live code."""
    outcome = await _execute_span_internal(
        span_id,
        source_db=source_db,
        sink_db=sink_db,
    )
    return outcome.result
