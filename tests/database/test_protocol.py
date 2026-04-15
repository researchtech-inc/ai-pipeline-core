"""Tests for canonical database protocols."""

import inspect
from datetime import datetime, timedelta
from typing import get_type_hints
from uuid import UUID

from ai_pipeline_core.database import (
    CostTotals,
    DatabaseReader,
    DocumentRecord,
    HydratedDocument,
    LogRecord,
    SpanRecord,
)
from ai_pipeline_core.database._memory import _MemoryDatabase
from ai_pipeline_core.database._protocol import DatabaseWriter
from ai_pipeline_core.database._types import _BlobRecord


async def _async_method(*args: object, **kwargs: object) -> object:
    return None


def _make_reader_stub() -> object:
    method_names = {
        "get_all_document_shas_for_tree",
        "get_blob",
        "get_blobs_batch",
        "get_cached_completion",
        "get_child_spans",
        "get_deployment_by_run_id",
        "get_deployment_cost_totals",
        "get_deployment_latest_activity",
        "get_deployment_logs",
        "get_deployment_logs_batch",
        "get_deployment_scoped_spans",
        "get_deployment_span_count",
        "get_deployment_tree",
        "get_deployment_tree_topology",
        "get_document",
        "get_document_events",
        "get_document_producers",
        "get_document_with_content",
        "get_documents_batch",
        "get_span",
        "get_span_logs",
        "get_spans_referencing_document",
        "latest_span_activity_for_deployment",
        "list_deployment_summaries",
        "list_deployments",
        "list_deployments_by_run_id",
        "list_running_deployment_roots",
        "list_tree_deployments",
        "find_documents_by_name",
    }
    return type("ReaderStub", (), {name: _async_method for name in method_names})()


def _make_writer_stub() -> object:
    namespace = {
        "supports_remote": property(lambda self: False),
        "insert_span": _async_method,
        "save_document": _async_method,
        "save_document_batch": _async_method,
        "save_blob": _async_method,
        "save_blob_batch": _async_method,
        "save_logs_batch": _async_method,
        "update_document_summary": _async_method,
        "flush": _async_method,
        "shutdown": _async_method,
    }
    return type("WriterStub", (), namespace)()


def _assert_signature(
    protocol: type[object],
    method_name: str,
    *,
    parameter_types: dict[str, object],
    return_type: object,
    keyword_only: set[str] | None = None,
) -> None:
    signature = inspect.signature(getattr(protocol, method_name))
    hints = get_type_hints(getattr(protocol, method_name))
    keyword_only = keyword_only or set()
    assert tuple(signature.parameters) == ("self", *parameter_types)
    for parameter_name, expected_annotation in parameter_types.items():
        parameter = signature.parameters[parameter_name]
        if parameter_name in keyword_only:
            assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
        assert hints[parameter_name] == expected_annotation
    assert hints["return"] == return_type


def test_memory_database_conforms_to_protocols() -> None:
    database = _MemoryDatabase()
    assert isinstance(database, DatabaseReader)
    assert isinstance(database, DatabaseWriter)
    assert database.supports_remote is False


def test_database_reader_is_runtime_checkable() -> None:
    assert getattr(DatabaseReader, "_is_runtime_protocol", False) is True
    assert isinstance(_make_reader_stub(), DatabaseReader)
    assert not isinstance(object(), DatabaseReader)


def test_database_writer_is_runtime_checkable() -> None:
    assert getattr(DatabaseWriter, "_is_runtime_protocol", False) is True
    assert isinstance(_make_writer_stub(), DatabaseWriter)
    assert not isinstance(object(), DatabaseWriter)


def test_database_reader_method_signatures() -> None:
    _assert_signature(DatabaseReader, "get_span", parameter_types={"span_id": UUID}, return_type=SpanRecord | None)
    _assert_signature(
        DatabaseReader,
        "get_document",
        parameter_types={"document_sha256": str},
        return_type=DocumentRecord | None,
    )
    _assert_signature(
        DatabaseReader,
        "get_document_with_content",
        parameter_types={"document_sha256": str},
        return_type=HydratedDocument | None,
    )
    _assert_signature(
        DatabaseReader,
        "get_blob",
        parameter_types={"content_sha256": str},
        return_type=_BlobRecord | None,
    )
    _assert_signature(
        DatabaseReader,
        "get_deployment_cost_totals",
        parameter_types={"root_deployment_id": UUID},
        return_type=CostTotals,
    )
    _assert_signature(
        DatabaseReader,
        "get_deployment_tree_topology",
        parameter_types={"root_deployment_id": UUID},
        return_type=list[SpanRecord],
    )
    _assert_signature(
        DatabaseReader,
        "get_deployment_latest_activity",
        parameter_types={"root_deployment_id": UUID},
        return_type=datetime | None,
    )
    _assert_signature(
        DatabaseReader,
        "get_cached_completion",
        parameter_types={"cache_key": str, "max_age": timedelta | None},
        return_type=SpanRecord | None,
        keyword_only={"max_age"},
    )
    _assert_signature(
        DatabaseReader,
        "list_running_deployment_roots",
        parameter_types={"limit": int},
        return_type=list[SpanRecord],
        keyword_only={"limit"},
    )
    _assert_signature(
        DatabaseReader,
        "latest_span_activity_for_deployment",
        parameter_types={"root_deployment_id": UUID},
        return_type=datetime | None,
    )
    _assert_signature(
        DatabaseReader,
        "list_deployments",
        parameter_types={"limit": int, "status": str | None, "root_only": bool, "offset": int},
        return_type=list[SpanRecord],
        keyword_only={"status", "root_only", "offset"},
    )
    _assert_signature(
        DatabaseReader,
        "list_deployments_by_run_id",
        parameter_types={"run_id": str},
        return_type=list[SpanRecord],
    )
    _assert_signature(
        DatabaseReader,
        "get_span_logs",
        parameter_types={"span_id": UUID, "level": str | None, "category": str | None},
        return_type=list[LogRecord],
        keyword_only={"level", "category"},
    )
    _assert_signature(
        DatabaseReader,
        "get_deployment_logs_batch",
        parameter_types={"deployment_ids": list[UUID], "level": str | None, "category": str | None},
        return_type=list[LogRecord],
        keyword_only={"level", "category"},
    )
    _assert_signature(
        DatabaseReader,
        "find_documents_by_name",
        parameter_types={"names": list[str], "document_type": str | None},
        return_type=dict[str, DocumentRecord],
        keyword_only={"document_type"},
    )


def test_database_writer_method_signatures() -> None:
    _assert_signature(DatabaseWriter, "insert_span", parameter_types={"span": SpanRecord}, return_type=type(None))
    _assert_signature(DatabaseWriter, "save_document", parameter_types={"record": DocumentRecord}, return_type=type(None))
    _assert_signature(DatabaseWriter, "save_blob", parameter_types={"blob": _BlobRecord}, return_type=type(None))
    _assert_signature(DatabaseWriter, "save_logs_batch", parameter_types={"logs": list[LogRecord]}, return_type=type(None))
