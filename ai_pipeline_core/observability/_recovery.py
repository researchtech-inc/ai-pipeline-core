"""Recovery helpers for orphaned running deployment trees."""

import logging
from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Protocol

from ai_pipeline_core.database import SpanRecord, SpanStatus
from ai_pipeline_core.database._json_helpers import json_dumps, parse_json_object
from ai_pipeline_core.database._protocol import DatabaseReader, DatabaseWriter
from ai_pipeline_core.deployment._types import ErrorCode
from ai_pipeline_core.settings import settings

__all__ = ["recover_orphaned_spans"]

logger = logging.getLogger(__name__)

_CRASH_ERROR_TYPE = "ProcessCrashed"
_CRASH_ERROR_MESSAGE = "Deployment process crashed or stopped before recording terminal span state."


class _RecoveryDatabase(DatabaseReader, DatabaseWriter, Protocol):
    """Database protocol used by orphan recovery."""


async def recover_orphaned_spans(
    database: _RecoveryDatabase,
    *,
    older_than: datetime | None = None,
    max_age_minutes: int | None = None,
    limit: int = 1000,
    is_alive: Callable[[SpanRecord], Awaitable[bool]] | None = None,
) -> list[str]:
    """Mark old running root deployments as failed so reconstruction sees them as terminal."""
    age_minutes = max_age_minutes if max_age_minutes is not None else settings.orphan_span_max_age_minutes
    cutoff = older_than or (datetime.now(UTC) - timedelta(minutes=age_minutes))
    recovered_run_ids: list[str] = []
    orphan_roots = await database.list_orphaned_deployment_roots(older_than=cutoff, limit=limit)
    for root_span in orphan_roots:
        if is_alive is not None and await is_alive(root_span):
            continue
        tree = await database.get_deployment_tree(root_span.root_deployment_id)
        ended_at = datetime.now(UTC)
        for span in tree:
            if span.status != SpanStatus.RUNNING:
                continue
            meta = parse_json_object(span.meta_json, context=f"Span {span.span_id}", field_name="meta_json") if span.meta_json else {}
            if span.span_id == root_span.span_id:
                meta["error_code"] = ErrorCode.CRASHED
            await database.insert_span(
                replace(
                    span,
                    status=SpanStatus.FAILED,
                    ended_at=ended_at,
                    version=span.version + 1,
                    error_type=_CRASH_ERROR_TYPE,
                    error_message=_CRASH_ERROR_MESSAGE,
                    meta_json=json_dumps(meta),
                )
            )
        logger.error(
            "reconcile.orphaned_run",
            extra={
                "event_type": "reconcile.orphaned_run",
                "run_id": root_span.run_id,
                "root_deployment_id": str(root_span.root_deployment_id),
                "deployment_id": str(root_span.deployment_id),
                "span_id": str(root_span.span_id),
            },
        )
        recovered_run_ids.append(root_span.run_id)
    return recovered_run_ids
