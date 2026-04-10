"""Tests for execution-context log field injection."""

import inspect
import json
import logging
import re
from types import MappingProxyType

from ai_pipeline_core.deployment._types import _NoopPublisher
from ai_pipeline_core.logger._context_filter import ExecutionContextFilter
from ai_pipeline_core.logger._json_formatter import JSONConsoleFormatter, _EXECUTION_FIELDS
from ai_pipeline_core.pipeline._execution_context import ExecutionContext, set_execution_context
from ai_pipeline_core.pipeline.limits import _SharedStatus


def _make_context(*, service_name: str) -> ExecutionContext:
    return ExecutionContext(
        run_id="test-run",
        execution_id=None,
        publisher=_NoopPublisher(),
        limits=MappingProxyType({}),
        limits_status=_SharedStatus(),
        service_name=service_name,
    )


def test_filter_injects_service_from_execution_context() -> None:
    record = logging.LogRecord("test", logging.INFO, __file__, 1, "message", (), None)

    with set_execution_context(_make_context(service_name="research")):
        ExecutionContextFilter().filter(record)

    assert record.service == "research"


def test_filter_sets_empty_service_when_not_set() -> None:
    record = logging.LogRecord("test", logging.INFO, __file__, 1, "message", (), None)

    with set_execution_context(_make_context(service_name="")):
        ExecutionContextFilter().filter(record)

    assert record.service == ""


def test_json_formatter_emits_service_field() -> None:
    record = logging.LogRecord("test", logging.INFO, __file__, 1, "message", (), None)
    record.service = "research"

    payload = json.loads(JSONConsoleFormatter().format(record))

    assert payload["service"] == "research"


def test_execution_context_filter_and_json_formatter_field_keys_match() -> None:
    source = inspect.getsource(ExecutionContextFilter.filter)
    filter_keys = set(re.findall(r'"([a-z_]+)"\s*:', source))

    assert filter_keys <= set(_EXECUTION_FIELDS)
