"""JSON stdout formatter for GCP-style structured logs."""

import json
import logging
import traceback
from datetime import UTC, datetime
from typing import Any, override

__all__ = ["JSONConsoleFormatter"]

_EXECUTION_FIELDS = (
    "run_id",
    "deployment_id",
    "root_deployment_id",
    "deployment_name",
    "span_id",
    "span_kind",
    "span_name",
    "span_target",
    "flow_name",
    "task_name",
    "service",
)


class JSONConsoleFormatter(logging.Formatter):
    """Render log records as JSON objects suitable for stdout shipping."""

    @override
    def format(self, record: logging.LogRecord) -> str:
        """Serialize one record as a JSON object."""
        payload: dict[str, Any] = {
            "severity": record.levelname,
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "logger": record.name,
            "message": record.getMessage(),
            "event_type": str(getattr(record, "event_type", "")),
        }
        for field_name in _EXECUTION_FIELDS:
            if hasattr(record, field_name):
                payload[field_name] = getattr(record, field_name)
        if record.exc_info is not None and record.exc_info[0] is not None:
            payload["exception"] = "".join(traceback.format_exception(*record.exc_info))
        return json.dumps(payload, default=str, sort_keys=True)
