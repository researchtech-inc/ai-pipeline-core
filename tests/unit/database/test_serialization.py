"""Tests for the shared span/log row deserialization helpers."""

from datetime import UTC, datetime
from uuid import uuid4

from ai_pipeline_core.database._serialization import SPAN_COLUMNS, row_to_span
from ai_pipeline_core.database._types import SpanKind, SpanStatus


def _span_row(*, kind: str, status: str) -> tuple[object, ...]:
    """Build a ClickHouse-shaped span row tuple aligned with SPAN_COLUMNS."""
    deployment_id = uuid4()
    values: dict[str, object] = {
        "span_id": uuid4(),
        "parent_span_id": None,
        "deployment_id": deployment_id,
        "root_deployment_id": deployment_id,
        "run_id": "run-123",
        "deployment_name": "example",
        "kind": kind,
        "name": "SpanName",
        "description": "",
        "status": status,
        "sequence_no": 0,
        "started_at": datetime.now(UTC),
        "ended_at": None,
        "version": 1,
        "cache_key": "",
        "previous_conversation_id": None,
        "cost_usd": 0.0,
        "error_type": "",
        "error_message": "",
        "input_document_shas": [],
        "output_document_shas": [],
        "target": "",
        "receiver_json": "",
        "input_json": "",
        "output_json": "",
        "error_json": "",
        "meta_json": "",
        "metrics_json": "",
        "input_blob_shas": [],
        "output_blob_shas": [],
    }
    assert tuple(values) == SPAN_COLUMNS  # guard against column drift
    return tuple(values[column] for column in SPAN_COLUMNS)


def test_row_to_span_round_trips_known_kind_and_status() -> None:
    span = row_to_span(_span_row(kind=SpanKind.PROMPT_EXECUTION, status=SpanStatus.COMPLETED))
    assert span.kind == SpanKind.PROMPT_EXECUTION
    assert span.status == SpanStatus.COMPLETED


def test_row_to_span_tolerates_unknown_future_kind_and_status() -> None:
    """Reading a row written by a newer framework must not raise.

    Regression for the production 500 where an older reader hard-failed on a
    span kind ('prompt_execution') it did not yet know. The read path must
    preserve unknown kind/status values as plain strings instead of crashing.
    """
    span = row_to_span(_span_row(kind="future_kind", status="future_status"))

    assert span.kind == "future_kind"
    assert span.status == "future_status"
    # Unknown values still behave as ordinary strings against the StrEnum set.
    assert span.kind not in {SpanKind.DEPLOYMENT, SpanKind.ATTEMPT}
    assert f"{span.kind}_output" == "future_kind_output"


def test_row_to_span_decodes_bytes_kind_and_status() -> None:
    """clickhouse-connect may hand back String columns as bytes; decode_text handles it."""
    # bytes kind/status simulate clickhouse-connect returning raw String bytes
    span = row_to_span(_span_row(kind=b"future_kind", status=b"future_status"))  # type: ignore[arg-type]  # exercises decode_text on bytes columns
    assert span.kind == "future_kind"
    assert span.status == "future_status"
