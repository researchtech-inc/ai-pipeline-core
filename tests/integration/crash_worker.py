"""Helper process for crash-resilience integration tests."""

import argparse
import asyncio
import signal
from pathlib import Path
from unittest.mock import patch
from uuid import UUID, uuid4

from ai_pipeline_core import DeploymentPlan, DeploymentResult, Document, FlowOptions, FlowStep, PipelineDeployment, PipelineFlow
from ai_pipeline_core.database.filesystem._backend import FilesystemDatabase


class _CrashInputDoc(Document):
    """Input document for the crash worker deployment."""


class _CrashOutputDoc(Document):
    """Output document for the crash worker deployment."""


class _CrashFlow(PipelineFlow):
    """Long-running flow used to exercise shutdown behavior."""

    async def run(self, input_docs: tuple[_CrashInputDoc, ...], options: FlowOptions) -> tuple[_CrashOutputDoc, ...]:
        _ = options
        await asyncio.sleep(30)
        return (_CrashOutputDoc.derive(derived_from=(input_docs[0],), name="done.txt", content="done"),)


class _CrashResult(DeploymentResult):
    """Deployment result for the crash worker."""


class _CrashDeployment(PipelineDeployment[FlowOptions, _CrashResult]):
    """Deployment used by crash-resilience integration tests."""

    flow_retries = 0

    def build_plan(self, options: FlowOptions) -> DeploymentPlan:
        _ = options
        return DeploymentPlan(steps=(FlowStep(_CrashFlow()),))

    @staticmethod
    def build_result(run_id: str, documents: tuple[Document, ...], options: FlowOptions) -> _CrashResult:
        _ = run_id, documents, options
        return _CrashResult(success=True)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db-path", type=Path, required=True)
    parser.add_argument("--mode", choices=("sigterm-flow", "sigkill-flow"), required=True)
    return parser.parse_args()


class _FakeFlowRun:
    def __init__(self, flow_run_id: UUID) -> None:
        self._flow_run_id = flow_run_id

    def get_id(self) -> UUID:
        return self._flow_run_id

    def __bool__(self) -> bool:
        return True


async def _run_worker(db_path: Path, mode: str) -> int:
    db_path.mkdir(parents=True, exist_ok=True)
    database = FilesystemDatabase(db_path, read_only=False)
    prefect_flow_run_id = uuid4()
    deployment_task = asyncio.create_task(
        _run_deployment(
            database=database,
            prefect_flow_run_id=prefect_flow_run_id,
        )
    )

    if mode == "sigterm-flow":
        loop = asyncio.get_running_loop()

        def _cancel_task() -> None:
            deployment_task.cancel()

        try:
            loop.add_signal_handler(signal.SIGTERM, _cancel_task)
        except NotImplementedError:
            signal.signal(signal.SIGTERM, lambda _signum, _frame: deployment_task.cancel())

    try:
        await deployment_task
    except asyncio.CancelledError:
        return 0
    finally:
        await database.shutdown()
    return 0


async def _run_deployment(*, database: FilesystemDatabase, prefect_flow_run_id: UUID) -> None:
    with patch("ai_pipeline_core.deployment.base.runtime.flow_run", new=_FakeFlowRun(prefect_flow_run_id)):
        await _CrashDeployment().run(
            "crash-worker-run",
            [_CrashInputDoc.create_root(name="input.txt", content="input", reason="crash-worker")],
            FlowOptions(),
            database=database,
        )


def main() -> int:
    args = _parse_args()
    return asyncio.run(_run_worker(args.db_path, args.mode))


if __name__ == "__main__":
    raise SystemExit(main())
