"""Request-scoped routing state for LLM fallback and deployment skipping."""

import time
from collections.abc import Iterator
from contextvars import ContextVar, Token
from dataclasses import replace
from uuid import uuid4

from ._aipl import AIPLResponseHeaders
from .exceptions import LLMError
from .model_response import AttemptOutcome
from .request import AttemptRequest, LLMRequest
from .types import AIModel

__all__ = ["GroupSkipList", "_CallContext", "reset_workload_state", "set_workload_state"]

WORKLOAD_SKIP_SECONDS = 120.0
WORKLOAD_DEMOTE_AFTER = 2
type WorkloadState = dict[tuple[str, str], tuple[int, float]]

# Workload demotion state lives in a ContextVar set by the integrating
# package (``ai_pipeline_core.pipeline._execution_context``) on every
# execution-scope entry. Outside that scope (and in standalone use) the
# default is None and demotion is a no-op.
_workload_state_cv: ContextVar[WorkloadState | None] = ContextVar(
    "_llm_core_workload_state",
    default=None,
)


def set_workload_state(state: WorkloadState | None) -> Token[WorkloadState | None]:
    """Install the workload demotion state for the current context.

    Called by ``ExecutionContext`` on entry so the framework's per-run state
    flows into ``_llm_core`` without ``_llm_core`` importing pipeline code.
    ``state=None`` disables demotion (standalone use, tests outside any
    execution context).
    """
    return _workload_state_cv.set(state)


def reset_workload_state(token: Token[WorkloadState | None]) -> None:
    """Restore the workload demotion state for the current context."""
    _workload_state_cv.reset(token)


class GroupSkipList:
    """Request-scoped deployment exclusion list."""

    def __init__(self) -> None:
        self.ids: frozenset[str] = frozenset()
        self.exhausted = False

    def update(self, new_ids: frozenset[str]) -> None:
        """Merge deployment ids into the skip list."""
        if new_ids:
            self.ids |= new_ids

    def merge_from_headers(self, headers: AIPLResponseHeaders) -> None:
        """Merge failed deployments from proxy response headers."""
        self.update(frozenset(headers.failed_deployments))
        if headers.group_status == "exhausted":
            self.exhausted = True


def derive_workload_id(*, run_id: str | None, name: str) -> str:
    """Derive the workload id used for proxy routing heuristics."""
    return f"{run_id}:{name}" if run_id else f"adhoc:{name}"


def mark_slow(workload_id: str, deployment_id: str) -> None:
    """Record a slow/bad deployment sample for this execution's workload."""
    state = _workload_state()
    if state is None:
        return
    key = (workload_id, deployment_id)
    consec_bad, skip_until = state.get(key, (0, 0.0))
    consec_bad += 1
    if consec_bad >= WORKLOAD_DEMOTE_AFTER:
        skip_until = time.monotonic() + WORKLOAD_SKIP_SECONDS
    state[key] = (consec_bad, skip_until)


def record_success(workload_id: str, deployment_id: str | None) -> None:
    """Clear workload-local bad streak after a successful call."""
    if deployment_id is None:
        return
    state = _workload_state()
    if state is None:
        return
    key = (workload_id, deployment_id)
    entry = state.get(key)
    if entry is None:
        return
    _consec_bad, skip_until = entry
    if skip_until > 0.0:
        state[key] = (0, skip_until)
    else:
        state.pop(key, None)


def skip_ids(workload_id: str) -> frozenset[str]:
    """Return active workload-local deployment skips."""
    state = _workload_state()
    if state is None:
        return frozenset()
    now = time.monotonic()
    expired = [key for key, (_consec_bad, skip_until) in state.items() if skip_until and skip_until <= now]
    for key in expired:
        state.pop(key, None)
    return frozenset(
        deployment_id
        for (wid, deployment_id), (_consec_bad, skip_until) in state.items()
        if wid == workload_id and skip_until > now
    )


def _workload_state() -> WorkloadState | None:
    return _workload_state_cv.get()


class _CallContext:
    """Mutable state for one logical generate call."""

    def __init__(self, request: LLMRequest) -> None:
        self.request = request
        self.skip = GroupSkipList()
        self.last_error: BaseException | None = None
        self.workload_id = request.routing.workload_id or derive_workload_id(
            run_id=None, name=request.purpose or request.model.name
        )
        self.skip.update(skip_ids(self.workload_id))

    def model_chain(self) -> Iterator[AIModel]:
        """Yield the active model and each fallback model."""
        current: AIModel | None = self.request.model
        while current is not None:
            yield current
            current = current.fallback

    def build_attempt(self, model: AIModel, attempt_index: int) -> AttemptRequest:
        """Build one attempt request with current skip state."""
        routing = replace(self.request.routing, skip_ids=self.skip.ids)
        call = replace(self.request, routing=routing)
        return AttemptRequest(
            call=call,
            model=model,
            attempt_index=attempt_index,
            call_id=uuid4().hex,
        )

    def absorb(self, outcome: AttemptOutcome) -> None:
        """Fold an attempt outcome into context state."""
        if outcome.error is not None:
            self.last_error = outcome.error
        if outcome.headers is not None:
            self.skip.merge_from_headers(outcome.headers)
        if outcome.new_skip_ids:
            self.skip.update(outcome.new_skip_ids)
        response = outcome.response
        if response is not None:
            deployment_id = response.transport.aipl.deployment_id
            record_success(self.workload_id, deployment_id)
            self.skip.update(frozenset(response.transport.aipl.failed_deployments))
            if response.transport.aipl.group_status == "exhausted":
                self.skip.exhausted = True
        if outcome.demote_workload and outcome.failed_deployment_id is not None:
            mark_slow(self.workload_id, outcome.failed_deployment_id)

    def terminal_error(self) -> BaseException:
        """Return the most useful terminal error after all attempts fail."""
        return self.last_error or LLMError(f"Exhausted AIModel chain for '{self.request.model.name}'.")
