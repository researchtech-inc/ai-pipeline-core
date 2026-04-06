"""Parallel execution primitives for pipeline tasks."""

import asyncio
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, TypeIs, cast

from ai_pipeline_core.documents import Document

__all__ = [
    "TaskBatch",
    "TaskHandle",
    "as_task_completed",
    "collect_tasks",
]

logger = logging.getLogger(__name__)
_DEFAULT_FAN_OUT_WARNING_THRESHOLD = 50


@dataclass(frozen=True, slots=True, eq=False)
class TaskHandle[T]:
    """Handle for an executing pipeline task."""

    _task: asyncio.Task[T]
    task_class: type[Any] | None
    input_arguments: Mapping[str, Any]

    def __await__(self):
        return self._task.__await__()

    @property
    def done(self) -> bool:
        """Whether the underlying task has finished."""
        return self._task.done()

    async def result(self) -> T:
        """Await the underlying task result."""
        return await self._task

    def cancel(self) -> None:
        """Cancel the underlying task."""
        self._task.cancel()


TaskAwaitable = TaskHandle[tuple[Document[Any], ...]] | Awaitable[tuple[Document[Any], ...]]
TaskAwaitableGroup = TaskAwaitable | Sequence[TaskAwaitable]


@dataclass(frozen=True, slots=True)
class TaskBatch:
    """Collected task results and handles that did not complete successfully."""

    completed: list[tuple[Document[Any], ...]]
    incomplete: list[TaskHandle[tuple[Document[Any], ...]]]


def _empty_arguments() -> Mapping[str, Any]:
    return MappingProxyType({})


def _to_handle(awaitable: TaskAwaitable) -> TaskHandle[tuple[Document[Any], ...]]:
    if isinstance(awaitable, TaskHandle):
        return awaitable
    task = asyncio.ensure_future(awaitable)
    return TaskHandle(
        _task=task,
        task_class=None,
        input_arguments=_empty_arguments(),
    )


def _is_handle_sequence(value: TaskAwaitableGroup) -> TypeIs[Sequence[TaskAwaitable]]:
    return isinstance(value, Sequence) and not isinstance(value, (str, bytes, TaskHandle))


def _normalize_handles(handles: tuple[TaskAwaitableGroup, ...]) -> list[TaskHandle[tuple[Document[Any], ...]]]:
    if not handles:
        return []
    if len(handles) == 1 and _is_handle_sequence(handles[0]):
        return [_to_handle(handle) for handle in handles[0]]

    flattened: list[TaskAwaitable] = []
    for handle in handles:
        if _is_handle_sequence(handle):
            flattened.extend(handle)
            continue
        flattened.append(cast("TaskAwaitable", handle))
    return [_to_handle(handle) for handle in flattened]


async def collect_tasks(
    *handles: TaskAwaitableGroup,
    deadline_seconds: float | None = None,
) -> TaskBatch:
    """Await task handles with an optional deadline and split completed/incomplete."""
    ordered_handles = _normalize_handles(handles)
    if not ordered_handles:
        return TaskBatch(completed=[], incomplete=[])
    if len(ordered_handles) > _DEFAULT_FAN_OUT_WARNING_THRESHOLD:
        logger.warning(
            "collect_tasks() received %d handles, which exceeds the global warning threshold of %d. "
            "If this fan-out is intentional, declare max_fan_out = %d on the flow class to document it. "
            "In Phase 1, max_fan_out is informational only and does not change the warning threshold.",
            len(ordered_handles),
            _DEFAULT_FAN_OUT_WARNING_THRESHOLD,
            len(ordered_handles),
        )

    completed: list[tuple[Document[Any], ...]] = []
    incomplete: list[TaskHandle[tuple[Document[Any], ...]]] = []
    by_task: dict[asyncio.Task[tuple[Document[Any], ...]], TaskHandle[tuple[Document[Any], ...]]] = {handle._task: handle for handle in ordered_handles}
    pending: set[asyncio.Task[tuple[Document[Any], ...]]] = set(by_task.keys())
    deadline_at = (time.monotonic() + deadline_seconds) if deadline_seconds is not None else None

    while pending:
        timeout: float | None = None
        if deadline_at is not None:
            timeout = max(0.0, deadline_at - time.monotonic())
            if timeout <= 0.0:
                break
        done, pending = await asyncio.wait(pending, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
        if not done:
            break
        for finished in done:
            handle = by_task[finished]
            outcome = (await asyncio.gather(handle.result(), return_exceptions=True))[0]
            if isinstance(outcome, BaseException):
                incomplete.append(handle)
                continue
            completed.append(outcome)

    incomplete.extend(by_task[still_pending] for still_pending in pending)
    return TaskBatch(completed=completed, incomplete=incomplete)


async def as_task_completed(*handles: TaskAwaitableGroup) -> AsyncIterator[TaskHandle[tuple[Document[Any], ...]]]:
    """Yield task handles in completion order."""
    ordered_handles = _normalize_handles(handles)
    if not ordered_handles:
        return

    by_task: dict[asyncio.Task[tuple[Document[Any], ...]], TaskHandle[tuple[Document[Any], ...]]] = {handle._task: handle for handle in ordered_handles}
    pending: set[asyncio.Task[tuple[Document[Any], ...]]] = set(by_task.keys())
    while pending:
        done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
        for finished in done:
            yield by_task[finished]
