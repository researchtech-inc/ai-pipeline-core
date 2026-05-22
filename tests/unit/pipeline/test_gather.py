"""Tests for safe_gather and safe_gather_indexed parallel execution primitives."""

import asyncio
import logging

import pytest

from ai_pipeline_core.pipeline import safe_gather, safe_gather_indexed


async def _succeed(value: int) -> int:
    return value


async def _fail(msg: str = "boom") -> int:
    raise RuntimeError(msg)


async def _cancel() -> int:
    raise asyncio.CancelledError()


class TestSafeGather:
    async def test_all_succeed(self):
        results = await safe_gather(_succeed(1), _succeed(2), _succeed(3))
        assert sorted(results) == [1, 2, 3]

    async def test_some_fail(self):
        results = await safe_gather(_succeed(1), _fail(), _succeed(3))
        assert sorted(results) == [1, 3]

    async def test_all_fail_raises(self):
        with pytest.raises(RuntimeError, match="All 2 tasks failed") as exc_info:
            await safe_gather(_fail("first"), _fail("second"))
        assert "first" in str(exc_info.value)

    async def test_all_fail_no_raise(self):
        results = await safe_gather(_fail(), _fail(), raise_if_all_fail=False)
        assert results == []

    async def test_cancelled_error_filtered(self):
        results = await safe_gather(_succeed(1), _cancel(), _succeed(3))
        assert sorted(results) == [1, 3]

    async def test_empty_input(self):
        results = await safe_gather()
        assert results == []

    async def test_label_in_error_message(self):
        with pytest.raises(RuntimeError, match="'my-label'"):
            await safe_gather(_fail(), label="my-label")

    async def test_failures_logged(self, caplog):
        with caplog.at_level(logging.WARNING):
            await safe_gather(_succeed(1), _fail("test_error"), raise_if_all_fail=False)
        assert "test_error" in caplog.text

    async def test_single_success(self):
        results = await safe_gather(_succeed(42))
        assert results == [42]

    async def test_single_failure_raises(self):
        with pytest.raises(RuntimeError, match="All 1 tasks failed"):
            await safe_gather(_fail())


class TestSafeGatherIndexed:
    async def test_all_succeed(self):
        results = await safe_gather_indexed(_succeed(1), _succeed(2), _succeed(3))
        assert results == [1, 2, 3]

    async def test_some_fail(self):
        results = await safe_gather_indexed(_succeed(1), _fail(), _succeed(3))
        assert results == [1, None, 3]

    async def test_preserves_order(self):
        results = await safe_gather_indexed(_succeed(10), _succeed(20), _succeed(30))
        assert results == [10, 20, 30]

    async def test_all_fail_raises(self):
        with pytest.raises(RuntimeError, match="All 2 tasks failed"):
            await safe_gather_indexed(_fail("first"), _fail("second"))

    async def test_all_fail_no_raise(self):
        results = await safe_gather_indexed(_fail(), _fail(), raise_if_all_fail=False)
        assert results == [None, None]

    async def test_cancelled_error_filtered(self):
        results = await safe_gather_indexed(_succeed(1), _cancel(), _succeed(3))
        assert results == [1, None, 3]

    async def test_empty_input(self):
        results = await safe_gather_indexed()
        assert results == []

    async def test_output_length_matches_input(self):
        results = await safe_gather_indexed(_succeed(1), _fail(), _succeed(3), _fail())
        assert len(results) == 4
        assert results[0] == 1
        assert results[1] is None
        assert results[2] == 3
        assert results[3] is None

    async def test_none_return_not_confused_with_failure(self):
        """A coroutine that succeeds with None should not trigger all-fail guard."""

        async def _return_none() -> None:
            return None

        # One success (None), one failure — should NOT raise
        results = await safe_gather_indexed(_return_none(), _fail())
        assert len(results) == 2
        assert results[0] is None  # success
        assert results[1] is None  # failure


class TestSafeGatherImports:
    def test_importable_from_pipeline(self):
        from ai_pipeline_core.pipeline import safe_gather, safe_gather_indexed

        assert callable(safe_gather)
        assert callable(safe_gather_indexed)

    def test_importable_from_top_level(self):
        from ai_pipeline_core import safe_gather, safe_gather_indexed

        assert callable(safe_gather)
        assert callable(safe_gather_indexed)
