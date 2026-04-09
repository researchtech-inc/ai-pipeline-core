"""CLI bootstrap for pipeline deployments.

Handles argument parsing and the Prefect test harness for local execution.
"""

import asyncio
import logging
import os
import sys
from collections.abc import Callable
from contextlib import ExitStack
from pathlib import Path
from typing import Any, cast

from prefect.logging import disable_run_logger
from prefect.testing.utilities import prefect_test_harness
from pydantic_settings import BaseSettings, CliPositionalArg, SettingsConfigDict

from ai_pipeline_core.database._factory import Database
from ai_pipeline_core.documents import Document
from ai_pipeline_core.logger._logging_config import setup_logging
from ai_pipeline_core.pipeline.options import FlowOptions
from ai_pipeline_core.settings import settings

from ._helpers import _create_publisher, _create_span_database_from_settings, build_auto_run_id, validate_run_id

logger = logging.getLogger(__name__)


async def _shutdown_database(database: Database) -> None:
    """Flush and shut down the CLI database."""
    try:
        await database.flush()
    except (OSError, RuntimeError, ValueError) as exc:
        logger.warning("Database flush failed: %s", exc)
    try:
        await database.shutdown()
    except (OSError, RuntimeError, ValueError) as exc:
        logger.warning("Database shutdown failed: %s", exc)


def run_cli_for_deployment(
    deployment: Any,
    initializer: Callable[..., tuple[str, tuple[Document, ...]]] | None = None,
    cli_mixin: type[BaseSettings] | None = None,
) -> None:
    """Execute pipeline from CLI arguments with --start/--end step control."""
    setup_logging()
    if len(sys.argv) == 1:
        sys.argv.append("--help")

    options_base = deployment.options_type
    if cli_mixin is not None:
        options_base = type(deployment.options_type)(
            "_OptionsBase",
            (cli_mixin, deployment.options_type),
            {"__module__": __name__, "__annotations__": {}},
        )

    cli_options_cls = type(
        "_CliOptions",
        (options_base, BaseSettings),
        {
            "__module__": __name__,
            "__annotations__": {
                "working_directory": CliPositionalArg[Path],
                "run_id": str | None,
                "start": int,
                "end": int | None,
            },
            "run_id": None,
            "start": 1,
            "end": None,
            "model_config": SettingsConfigDict(
                frozen=True,
                extra="ignore",
                cli_parse_args=True,
                cli_kebab_case=True,
                cli_exit_on_error=True,
                cli_prog_name=deployment.name,
                cli_use_class_docs_for_groups=True,
            ),
        },
    )

    cli_opts = cli_options_cls()
    # Project into the deployment's stable options type so the codec never sees the dynamic CLI class
    run_opts_payload = {name: getattr(cli_opts, name) for name in deployment.options_type.model_fields}
    opts = cast(FlowOptions, deployment.options_type.model_validate(run_opts_payload))

    wd = cast(Path, cli_opts.working_directory)
    wd.mkdir(parents=True, exist_ok=True)

    start_step = getattr(cli_opts, "start", 1)
    end_step = getattr(cli_opts, "end", None)

    initial_documents: tuple[Document, ...] = ()
    if initializer:
        init_name, initial_documents = initializer(opts)
        run_id = cast(str | None, cli_opts.run_id) or init_name
    else:
        run_id = cast(str | None, cli_opts.run_id)

    if not run_id:
        run_id = build_auto_run_id(output_dir_name=wd.name, documents=initial_documents, options=opts)

    validate_run_id(run_id)

    publisher = _create_publisher(settings, deployment.pubsub_service_type)
    database = _create_span_database_from_settings(settings, base_path=wd)

    try:
        with ExitStack() as stack:
            under_pytest = "PYTEST_CURRENT_TEST" in os.environ or "pytest" in sys.modules
            if not settings.prefect_api_key and not under_pytest:
                stack.enter_context(prefect_test_harness())
                stack.enter_context(disable_run_logger())

            try:
                result = asyncio.run(
                    deployment.run(
                        run_id=run_id,
                        documents=initial_documents,
                        options=opts,
                        publisher=publisher,
                        start_step=start_step,
                        end_step=end_step,
                        database=database,
                    )
                )
            finally:
                if hasattr(publisher, "close"):
                    asyncio.run(publisher.close())

        result_file = wd / "result.json"
        result_file.write_text(result.model_dump_json(indent=2))
        logger.info("Result saved to %s", result_file)

    finally:
        asyncio.run(_shutdown_database(database))
