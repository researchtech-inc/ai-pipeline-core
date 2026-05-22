"""Safe parallel execution primitives for pipeline tasks.

Wraps asyncio.gather with correct error handling for independent
parallel operations (LLM calls, document processing, etc.).
"""

import asyncio
import logging
from collections.abc import Coroutine
from typing import Any

logger = logging.getLogger(__name__)

__all__ = [
    "safe_gather",
    "safe_gather_indexed",
]


async def _execute_gather[T](
    *coroutines: Coroutine[Any, Any, T],
    label: str,
) -> tuple[list[Any], list[tuple[int, BaseException]]]:
    """Shared gather logic: execute with return_exceptions=True, separate successes from failures.

    Returns (raw_results, indexed_failures) where indexed_failures is [(index, exception), ...].
    """
    results = await asyncio.gather(*coroutines, return_exceptions=True)

    prefix = f"[{label}] " if label else ""
    failures: list[tuple[int, BaseException]] = []

    for i, result in enumerate(results):
        if isinstance(result, BaseException):
            logger.warning("%stask %d/%d failed: %s", prefix, i + 1, len(results), result)
            failures.append((i, result))

    return list(results), failures


async def safe_gather[T](
    *coroutines: Coroutine[Any, Any, T],
    label: str = "",
    raise_if_all_fail: bool = True,
) -> list[T]:
    """Execute coroutines in parallel, returning successes and logging failures.

    Uses return_exceptions=True internally. Filters failures with BaseException
    (catches CancelledError). Logs each failure with context.

    Returns:
        List of successful results (failures filtered out). Relative order of
        successes is preserved, but indices shift. Use safe_gather_indexed
        for positional correspondence.
    """
    if not coroutines:
        return []

    results, failures = await _execute_gather(*coroutines, label=label)
    failure_indices = {i for i, _ in failures}
    successes: list[T] = [r for i, r in enumerate(results) if i not in failure_indices]

    if not successes and raise_if_all_fail and failures:
        first_error = failures[0][1]
        raise RuntimeError(
            f"All {len(failures)} tasks failed{f' in {label!r}' if label else ''}. First error: {first_error}"
        ) from first_error

    return successes


async def safe_gather_indexed[T](
    *coroutines: Coroutine[Any, Any, T],
    label: str = "",
    raise_if_all_fail: bool = True,
) -> list[T | None]:
    """Execute coroutines in parallel, preserving positional correspondence.

    Like safe_gather, but returns a list with the same length as the input.
    Failed positions contain None. Useful when results must correspond to
    specific inputs by index.

    Returns:
        List matching input length. Successful results at their original index,
        None at positions where the coroutine failed.
    """
    if not coroutines:
        return []

    results, failures = await _execute_gather(*coroutines, label=label)
    failure_indices = {i for i, _ in failures}
    output: list[T | None] = [None if i in failure_indices else r for i, r in enumerate(results)]

    if len(failures) == len(results) and raise_if_all_fail:
        first_error = failures[0][1]
        raise RuntimeError(
            f"All {len(failures)} tasks failed{f' in {label!r}' if label else ''}. First error: {first_error}"
        ) from first_error

    return output
