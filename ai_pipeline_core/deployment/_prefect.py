"""Prefect flow construction for PipelineDeployment."""

import logging
from collections.abc import Sequence
from typing import Any, cast
from uuid import UUID

from prefect import flow

from ai_pipeline_core.pipeline import PipelineFlow
from ai_pipeline_core.pipeline.options import FlowOptions
from ai_pipeline_core.settings import settings

from ._helpers import _create_publisher, _create_span_database_from_settings
from ._resolve import _DocumentInput, resolve_document_inputs

__all__ = [
    "build_integration_meta",
    "build_prefect_flow",
]

logger = logging.getLogger(__name__)


def build_integration_meta(deployment: Any) -> dict[str, Any]:
    """Build deploy-time schema metadata from a deployment's flow graph."""
    flows: list[PipelineFlow]
    try:
        options = cast(FlowOptions, deployment.options_type.model_construct())
        plan = deployment.build_plan(options)
        flows = [step.flow for step in plan.steps]
    except Exception as exc:
        logger.warning("Failed to build flow metadata for %s: %s", type(deployment).__name__, exc)
        flows = []

    if not flows:
        return {
            "input_document_types": [],
            "all_document_types": [],
            "flow_chain": [],
        }

    first_flow = flows[0]
    input_types = list(type(first_flow).input_document_types)
    all_types = deployment._all_document_types(flows)

    return {
        "input_document_types": [{"class_name": t.__name__, "description": (t.__doc__ or "").strip()} for t in input_types],
        "all_document_types": [{"class_name": t.__name__, "description": (t.__doc__ or "").strip()} for t in all_types],
        "flow_chain": [
            {
                "name": flow_instance.name,
                "input_types": [t.__name__ for t in type(flow_instance).input_document_types],
                "output_types": [t.__name__ for t in type(flow_instance).output_document_types],
                "estimated_minutes": flow_instance.estimated_minutes,
            }
            for flow_instance in flows
        ],
    }


def build_prefect_flow(deployment: Any) -> Any:
    """Create the Prefect flow wrapper for a deployment."""

    async def _deployment_flow(
        run_id: str,
        document_inputs: list[_DocumentInput],
        options: FlowOptions,
        parent_execution_id: str | None = None,
        parent_deployment_task_id: str | None = None,
        root_deployment_id: str | None = None,
    ) -> Any:
        publisher = _create_publisher(settings, deployment.pubsub_service_type)
        database = _create_span_database_from_settings(settings)
        try:
            built_plan = deployment.build_plan(cast(Any, options))
            built_flows = list(cast(Sequence[PipelineFlow], [step.flow for step in built_plan.steps]))
            if not built_flows:
                raise ValueError(f"{type(deployment).__name__}.build_plan() returned an empty plan.")

            start_step_input_types = list(type(built_flows[0]).input_document_types)
            typed_docs = await resolve_document_inputs(
                document_inputs,
                deployment._all_document_types(built_flows),
                start_step_input_types=start_step_input_types,
            )
            parent_uuid = UUID(parent_execution_id) if parent_execution_id else None
            if root_deployment_id is not None or parent_deployment_task_id is not None:
                root_dep_uuid = UUID(root_deployment_id) if root_deployment_id else None
                parent_task_uuid = UUID(parent_deployment_task_id) if parent_deployment_task_id else None
                return await deployment._run_with_context(
                    run_id,
                    typed_docs,
                    cast(Any, options),
                    root_deployment_id=root_dep_uuid,
                    parent_deployment_task_id=parent_task_uuid,
                    publisher=publisher,
                    parent_execution_id=parent_uuid,
                    database=database,
                )
            return await deployment.run(
                run_id,
                typed_docs,
                cast(Any, options),
                publisher=publisher,
                parent_execution_id=parent_uuid,
                database=database,
            )
        finally:
            await publisher.close()
            await database.flush()
            await database.shutdown()

    _deployment_flow.__annotations__["options"] = deployment.options_type
    _deployment_flow.__annotations__["return"] = deployment.result_type
    deployment_flow = cast(Any, _deployment_flow)
    deployment_flow._integration_meta = build_integration_meta(deployment)

    return flow(
        name=deployment.name,
        flow_run_name=f"{deployment.name}-{{run_id}}",
        persist_result=True,
        result_serializer="json",
    )(_deployment_flow)
