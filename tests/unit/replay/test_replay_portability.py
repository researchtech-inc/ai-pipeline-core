"""Tests for replay portability across source and sink databases."""

import pytest

from ai_pipeline_core.database._types import _BlobRecord
from ai_pipeline_core.documents._hashing import compute_content_sha256
from ai_pipeline_core.replay import execute_span
from tests.unit.replay.conftest import ReplayTextDocument, make_span, store_document_in_database


async def portability_function(*, document: ReplayTextDocument, payload: bytes) -> str:
    """Replay target used to verify sink artifact copying."""
    return f"{document.name}:{len(payload)}"


@pytest.mark.asyncio
async def test_execute_span_copies_input_artifacts_when_source_and_sink_differ(
    memory_database,
    sample_text_doc: ReplayTextDocument,
) -> None:
    sink_database = type(memory_database)()
    await store_document_in_database(memory_database, sample_text_doc)
    payload = b"binary-payload"
    payload_sha = compute_content_sha256(payload)
    await memory_database.save_blob(_BlobRecord(content_sha256=payload_sha, content=payload))

    span = make_span(
        kind="task",
        name="portable",
        target=f"function:{__name__}:portability_function",
        input_value={"document": sample_text_doc, "payload": payload},
    )
    await memory_database.insert_span(span)

    result = await execute_span(
        span.span_id,
        source_db=memory_database,
        sink_db=sink_database,
    )

    assert result == f"{sample_text_doc.name}:{len(payload)}"
    assert await sink_database.get_document(sample_text_doc.sha256) is not None
    assert await sink_database.get_blob(payload_sha) is not None
