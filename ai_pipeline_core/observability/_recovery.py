"""Recovery helpers for orphaned running deployment trees."""

import logging
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Protocol
from uuid import UUID

import httpx
from prefect.exceptions import ObjectNotFound  # noqa: TID251 - NEXT-STEPS v2 requires module-level Prefect import here.

from ai_pipeline_core.database import SpanRecord, SpanStatus
from ai_pipeline_core.database._json_helpers import json_dumps, parse_json_object
from ai_pipeline_core.database._protocol import DatabaseReader, DatabaseWriter
from ai_pipeline_core.deployment._types import _CRASH_ERROR_TYPE, ErrorCode
from ai_pipeline_core.settings import settings

__all__ = ["recover_orphaned_spans"]

logger = logging.getLogger(__name__)

_CRASH_ERROR_MESSAGE = "Deployment process crashed or stopped before recording terminal span state."
_RUNNING_ROOT_SCAN_LIMIT = 1000
_SECONDS_PER_HOUR = 3600
_DATABASE_READ_ERRORS = (ConnectionError, OSError, TimeoutError)
_TRANSIENT_PREFECT_ERRORS = (ConnectionError, OSError, TimeoutError, httpx.HTTPError)
# Heartbeat and span activity are not reconciliation inputs. See 0.22.0 release notes.
# Durable liveness is a 0.23.0 schema change.
_MANUAL_RECONCILIATION_FALLBACK_MAX_HOURS = 48


class PrefectProbeKind(StrEnum):
    """Outcome kinds for probing Prefect about one deployment root."""

    TERMINAL_FAILED = "terminal_failed"
    TERMINAL_CRASHED = "terminal_crashed"
    TERMINAL_CANCELLED = "terminal_cancelled"
    TERMINAL_COMPLETED = "terminal_completed"
    RUNNING = "running"
    NOT_FOUND = "not_found"
    TRANSIENT_ERROR = "transient_error"
    MISSING_LINK = "missing_link"


@dataclass(frozen=True, slots=True)
class PrefectProbeResult:
    """Typed result of probing Prefect for one deployment root."""

    kind: PrefectProbeKind
    message: str
    updated_at: datetime | None
    raw_flow_run: Any | None


class ReconcileAction(StrEnum):
    """Possible reconciliation decisions for one root span."""

    NOOP = "noop"
    MARK_FAILED = "mark_failed"
    SKIP_TRANSIENT_ERROR = "skip_transient_error"
    REQUIRES_MANUAL_INTERVENTION = "requires_manual_intervention"


class _RecoveryDatabase(DatabaseReader, DatabaseWriter, Protocol):
    """Database protocol used by orphan recovery."""


def _extract_prefect_flow_run_id(root: SpanRecord) -> UUID | None:
    """Read Prefect flow-run ID from deployment root span metadata."""
    if not root.meta_json:
        return None
    meta = parse_json_object(root.meta_json, context=f"Span {root.span_id}", field_name="meta_json")
    raw_value = meta.get("prefect_flow_run_id")
    if not isinstance(raw_value, str) or not raw_value:
        return None
    try:
        return UUID(raw_value)
    except ValueError:
        return None


def _normalize_updated_at(flow_run: Any) -> datetime | None:
    updated_at = getattr(flow_run, "updated", None)
    if not isinstance(updated_at, datetime):
        return None
    if updated_at.tzinfo is None:
        return updated_at.replace(tzinfo=UTC)
    return updated_at


def _probe_kind_for_state(state_name: str) -> PrefectProbeKind:
    if state_name == "FAILED":
        return PrefectProbeKind.TERMINAL_FAILED
    if state_name == "CRASHED":
        return PrefectProbeKind.TERMINAL_CRASHED
    if state_name == "CANCELLED":
        return PrefectProbeKind.TERMINAL_CANCELLED
    if state_name == "COMPLETED":
        return PrefectProbeKind.TERMINAL_COMPLETED
    return PrefectProbeKind.RUNNING


async def _probe_prefect_for_root(
    prefect_client: Any,
    root: SpanRecord,
) -> PrefectProbeResult:
    flow_run_id = _extract_prefect_flow_run_id(root)
    if flow_run_id is None:
        return PrefectProbeResult(
            kind=PrefectProbeKind.MISSING_LINK,
            message=(
                f"Reconciler found root span `{root.span_id}` with no `prefect_flow_run_id` in `meta_json`. "
                "Leaving the span untouched because there is no orchestrator record to verify. "
                "Re-run with `ai-trace recover --i-accept-nondeterministic-reconciliation` only after operator investigation."
            ),
            updated_at=None,
            raw_flow_run=None,
        )

    try:
        flow_run = await prefect_client.read_flow_run(flow_run_id)
    except ObjectNotFound:
        return PrefectProbeResult(
            kind=PrefectProbeKind.NOT_FOUND,
            message=(
                f"Reconciler found root span `{root.span_id}` whose Prefect flow run `{flow_run_id}` returned ObjectNotFound. "
                "This may be a genuinely deleted run or a transient Prefect API failure. "
                "Leaving the span untouched. Run `ai-trace recover --i-accept-nondeterministic-reconciliation` "
                "to force wall-clock reconciliation."
            ),
            updated_at=None,
            raw_flow_run=None,
        )
    except _TRANSIENT_PREFECT_ERRORS as exc:
        return PrefectProbeResult(
            kind=PrefectProbeKind.TRANSIENT_ERROR,
            message=(
                f"Orphan reaper: Prefect lookup failed for root span `{root.span_id}`: {exc}. "
                "Leaving the span untouched to avoid false positives. Fix Prefect connectivity or auth and retry reconciliation."
            ),
            updated_at=None,
            raw_flow_run=None,
        )

    state_type = getattr(flow_run, "state_type", None)
    if state_type is None:
        state = getattr(flow_run, "state", None)
        if state is not None:
            state_type = getattr(state, "type", None)
    if state_type is None:
        state_name = ""
    else:
        state_value = getattr(state_type, "value", None)
        state_name = state_value if isinstance(state_value, str) else str(state_type)
    return PrefectProbeResult(
        kind=_probe_kind_for_state(state_name),
        message=f"prefect_state={state_name}",
        updated_at=_normalize_updated_at(flow_run),
        raw_flow_run=flow_run,
    )


def _decide_reconcile_action(
    root: SpanRecord,
    probe: PrefectProbeResult,
    latest_activity: datetime,
) -> ReconcileAction:
    _ = root, latest_activity
    if probe.kind in {
        PrefectProbeKind.TERMINAL_FAILED,
        PrefectProbeKind.TERMINAL_CRASHED,
        PrefectProbeKind.TERMINAL_CANCELLED,
    }:
        return ReconcileAction.MARK_FAILED
    if probe.kind == PrefectProbeKind.TERMINAL_COMPLETED:
        return ReconcileAction.REQUIRES_MANUAL_INTERVENTION
    if probe.kind == PrefectProbeKind.TRANSIENT_ERROR:
        return ReconcileAction.SKIP_TRANSIENT_ERROR
    if probe.kind in {PrefectProbeKind.NOT_FOUND, PrefectProbeKind.MISSING_LINK}:
        return ReconcileAction.REQUIRES_MANUAL_INTERVENTION
    return ReconcileAction.NOOP


def _updated_meta_json(span: SpanRecord, *, reason: str, error_code: ErrorCode | None) -> str:
    meta = parse_json_object(span.meta_json, context=f"Span {span.span_id}", field_name="meta_json") if span.meta_json else {}
    meta["reconcile_reason"] = reason
    if error_code is not None:
        meta["error_code"] = error_code
    return json_dumps(meta)


async def _mark_root_failed(
    database: _RecoveryDatabase,
    root: SpanRecord,
    *,
    reason: str,
    error_type: str,
) -> None:
    tree = await database.get_deployment_tree(root.root_deployment_id)
    ended_at = datetime.now(UTC)
    for span in tree:
        if span.status != SpanStatus.RUNNING:
            continue
        await database.insert_span(
            replace(
                span,
                status=SpanStatus.FAILED,
                ended_at=ended_at,
                version=span.version + 1,
                error_type=error_type,
                error_message=_CRASH_ERROR_MESSAGE,
                meta_json=_updated_meta_json(
                    span,
                    reason=reason,
                    error_code=ErrorCode.CRASHED if span.span_id == root.span_id else None,
                ),
            )
        )
    logger.error(
        "reconcile.orphaned_run",
        extra={
            "event_type": "reconcile.orphaned_run",
            "run_id": root.run_id,
            "root_deployment_id": str(root.root_deployment_id),
            "deployment_id": str(root.deployment_id),
            "span_id": str(root.span_id),
            "reap_reason": reason,
        },
    )


async def recover_orphaned_spans(
    database: _RecoveryDatabase,
    prefect_client: Any | None = None,
    *,
    fallback_max_hours: int | None = None,
    require_prefect_client: bool | None = None,
) -> list[UUID]:
    """Reconciles durable run state against orchestrator-reported terminal states.

    Formerly known as "orphan recovery"; the module will be renamed to
    `_reconcile.py` in 0.23.0 when downstream consumers have migrated.
    Only acts on explicit orchestrator-terminal signals unless the caller
    intentionally opts into nondeterministic wall-clock reconciliation.
    """
    caller_explicit_optout = require_prefect_client is False
    operator_preconfigured_optout = not settings.orphan_reap_require_prefect_client
    require_client = not (caller_explicit_optout and operator_preconfigured_optout)
    if prefect_client is None and require_client:
        raise RuntimeError(
            "recover_orphaned_spans() refuses to run without a Prefect client. "
            "Pass prefect_client=<PrefectClient> to reconcile from orchestrator truth, "
            "or explicitly opt into nondeterministic wall-clock reconciliation by passing "
            "BOTH require_prefect_client=False AND setting "
            "ORPHAN_REAP_REQUIRE_PREFECT_CLIENT=false in the environment "
            "(CLI: --i-accept-nondeterministic-reconciliation only sets the kwarg; "
            "the env var must also be set explicitly by the operator)."
        )

    manual_fallback_hours = fallback_max_hours if fallback_max_hours is not None else _MANUAL_RECONCILIATION_FALLBACK_MAX_HOURS
    running_roots = await database.list_running_deployment_roots(limit=_RUNNING_ROOT_SCAN_LIMIT)
    now = datetime.now(UTC)
    reconciled_roots: list[UUID] = []

    for root in running_roots:
        try:
            latest_activity = await database.latest_span_activity_for_deployment(root.span_id)
        except _DATABASE_READ_ERRORS as exc:
            logger.warning(
                "Orphan reaper: cannot read activity for %s (%s): %s. Leaving the span untouched for this cycle. "
                "Fix the database read path and retry reconciliation.",
                root.span_id,
                root.deployment_name or root.name,
                exc,
            )
            continue
        activity_timestamp = latest_activity or root.started_at

        if prefect_client is None:
            hours_since_activity = (now - activity_timestamp).total_seconds() / _SECONDS_PER_HOUR
            if hours_since_activity <= manual_fallback_hours:
                continue
            await _mark_root_failed(
                database,
                root,
                reason=f"manual_wall_clock_{int(hours_since_activity)}h",
                error_type=_CRASH_ERROR_TYPE,
            )
            reconciled_roots.append(root.span_id)
            continue

        probe = await _probe_prefect_for_root(prefect_client, root)
        action = _decide_reconcile_action(root, probe, activity_timestamp)

        if action == ReconcileAction.MARK_FAILED:
            await _mark_root_failed(database, root, reason=probe.message, error_type=_CRASH_ERROR_TYPE)
            reconciled_roots.append(root.span_id)
            continue

        if action == ReconcileAction.SKIP_TRANSIENT_ERROR:
            logger.warning("%s", probe.message)
            continue

        if action == ReconcileAction.REQUIRES_MANUAL_INTERVENTION:
            if probe.kind == PrefectProbeKind.TERMINAL_COMPLETED:
                logger.warning(
                    "Prefect reports COMPLETED for root span `%s` but durable spans are still RUNNING. "
                    "The worker likely crashed after Prefect saw success but before persistence finished. "
                    "Investigate actual output state before reconciling. "
                    "Re-run with `ai-trace recover --i-accept-nondeterministic-reconciliation` "
                    "only after confirming the run produced its expected artifacts.",
                    root.span_id,
                )
            else:
                logger.warning("%s", probe.message)
            continue

    return reconciled_roots
