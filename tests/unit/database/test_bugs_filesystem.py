"""Tests for filesystem backend fixes.

Covers:
- Write serialization via threading.Lock
- Append-only log writes
"""

import asyncio
import json
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4


from ai_pipeline_core.database._types import LogRecord
from ai_pipeline_core.database.filesystem._backend import FilesystemDatabase

_DEPLOY_ID = uuid4()
_SPAN_ID = uuid4()


def _make_fs_db(tmp_path: Path) -> FilesystemDatabase:
    return FilesystemDatabase(tmp_path / "test-deploy")


def _make_log(i: int) -> LogRecord:
    return LogRecord(
        deployment_id=_DEPLOY_ID,
        span_id=_SPAN_ID,
        timestamp=datetime.now(UTC),
        sequence_no=i,
        level="INFO",
        category="test",
        logger_name=f"test.module.{i}",
        message=f"Log message {i}",
    )


async def test_log_file_append_only_io() -> None:
    """_save_logs_batch_sync appends only new batch, not full accumulated logs."""
    from unittest.mock import patch

    with tempfile.TemporaryDirectory() as tmpdir:
        db = _make_fs_db(Path(tmpdir))

        total_bytes_written = 0

        original_open = Path.open

        def tracking_open(self_path: Path, *args: object, **kwargs: object):  # type: ignore[no-untyped-def]
            handle = original_open(self_path, *args, **kwargs)
            if str(self_path).endswith("logs.jsonl") and args and args[0] == "a":

                class TrackingWriter:
                    def __init__(self, h):  # type: ignore[no-untyped-def]
                        self._h = h

                    def write(self, data: str) -> int:
                        nonlocal total_bytes_written
                        total_bytes_written += len(data.encode("utf-8"))
                        return self._h.write(data)

                    def __enter__(self):  # type: ignore[no-untyped-def]
                        self._h.__enter__()
                        return self

                    def __exit__(self, *exc):  # type: ignore[no-untyped-def]
                        return self._h.__exit__(*exc)

                return TrackingWriter(handle)
            return handle

        batch_size = 10
        num_batches = 10

        with patch.object(Path, "open", tracking_open):
            for batch in range(num_batches):
                logs = [_make_log(batch * batch_size + i) for i in range(batch_size)]
                await db.save_logs_batch(logs)

        final_size = db._logs_path.stat().st_size
        ratio = total_bytes_written / final_size
        assert ratio < 2.0, (
            f"Total bytes written ({total_bytes_written}) is {ratio:.1f}x the final file size ({final_size}). Expected ratio < 2.0 for append-only writes."
        )


async def test_log_file_content_correct_after_multiple_batches() -> None:
    """All log messages must be present after multiple append batches."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db = _make_fs_db(Path(tmpdir))

        batch1 = [_make_log(i) for i in range(3)]
        batch2 = [_make_log(i + 3) for i in range(3)]
        await db.save_logs_batch(batch1)
        await db.save_logs_batch(batch2)

        content = db._logs_path.read_text().strip()
        lines = content.split("\n")
        messages = {json.loads(line)["message"] for line in lines if line.strip()}
        for i in range(6):
            assert f"Log message {i}" in messages


async def test_concurrent_log_writes_no_data_loss() -> None:
    """Parallel save_logs_batch calls must not lose data (write lock prevents corruption)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db = _make_fs_db(Path(tmpdir))
        num_tasks = 20

        batch_size = 5

        async def write_batch(task_id: int) -> None:
            logs = [_make_log(task_id * batch_size + i) for i in range(batch_size)]
            await db.save_logs_batch(logs)

        await asyncio.gather(*(write_batch(i) for i in range(num_tasks)))

        content = db._logs_path.read_text().strip()
        lines = [line for line in content.split("\n") if line.strip()]
        messages = {json.loads(line)["message"] for line in lines}
        expected = {f"Log message {i}" for i in range(num_tasks * batch_size)}
        missing = expected - messages
        assert not missing, f"Lost {len(missing)} log messages under concurrent writes: {sorted(missing)[:5]}..."
