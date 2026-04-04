"""Deterministic markdown bundle writing for trace-inspector."""

from __future__ import annotations

from pathlib import Path
from uuid import UUID

from trace_inspector._helpers import ensure_directory, prepare_output_dir, write_text
from trace_inspector._render import (
    build_batch_groups,
    render_batch_markdown,
    render_document_file,
    render_flow_markdown,
    render_index_markdown,
    render_task_markdown,
)
from trace_inspector._render_helpers import root_task_ids
from trace_inspector._types import LoadedTask, LoadedTrace, ProvenanceEntry, RenderConfig, Selection, snake_case_segment

__all__ = [
    "write_full_bundle",
]

type TaskFileMap = dict[UUID, str]


async def write_full_bundle(trace: LoadedTrace, provenance: dict[str, ProvenanceEntry], selection: Selection) -> None:
    """Write a full markdown inspection bundle for the selected run scope."""
    selected_flow_ids, selected_task_ids = _resolve_selection(trace, provenance, selection)
    task_file_map, flow_file_map = _build_file_maps(
        trace,
        selected_flow_ids,
        selected_task_ids,
        selection.render_config.batch_threshold,
    )
    selected_document_shas = _selected_document_shas(trace, selected_task_ids)

    await prepare_output_dir(selection.output_dir)
    await _write_documents(selection.output_dir / "docs", trace, provenance, selected_document_shas, selection.render_config)
    await ensure_directory(selection.output_dir / "flows")
    for flow_span in trace.flow_spans:
        if flow_span.span_id not in selected_flow_ids:
            continue
        await _write_flow_bundle(
            output_dir=selection.output_dir,
            trace=trace,
            provenance=provenance,
            flow_id=flow_span.span_id,
            selected_task_ids=selected_task_ids,
            task_file_map=task_file_map,
            flow_file_map=flow_file_map,
            selection=selection,
        )

    index_markdown = render_index_markdown(
        trace,
        selected_flow_ids=selected_flow_ids,
        selected_task_ids=selected_task_ids,
        flow_file_map=flow_file_map,
        task_file_map=task_file_map,
        batch_threshold=selection.render_config.batch_threshold,
    )
    await write_text(selection.output_dir / "index.md", index_markdown)


def _resolve_selection(
    trace: LoadedTrace,
    provenance: dict[str, ProvenanceEntry],
    selection: Selection,
) -> tuple[set[UUID], set[UUID]]:
    selected_flow_ids = {flow.span_id for flow in trace.flow_spans}
    selected_task_ids = set(trace.tasks)
    if selection.mode == "run":
        if selection.flow_name is not None:
            selected_flow_ids = {flow.span_id for flow in trace.flow_spans if flow.name == selection.flow_name}
            if not selected_flow_ids:
                available = sorted({flow.name for flow in trace.flow_spans})
                msg = (
                    f"No flow named {selection.flow_name!r} found in the deployment tree. "
                    f"Available flow names: {', '.join(available) or 'none'}. "
                    f"Pass a valid --flow-name or omit to render all flows."
                )
                raise ValueError(msg)
            selected_task_ids = {task_id for flow_id in selected_flow_ids for task_id in trace.tasks_by_flow.get(flow_id, ())}
        if selection.failed_only:
            selected_task_ids = {task_id for task_id in selected_task_ids if trace.tasks[task_id].span.status == "failed"}
            selected_flow_ids = {trace.tasks[task_id].parent_flow_span.span_id for task_id in selected_task_ids}
        return selected_flow_ids, selected_task_ids

    if selection.mode == "flow":
        if selection.flow_span_id is not None:
            if selection.flow_span_id not in trace.spans_by_id:
                msg = f"Flow span {selection.flow_span_id} was not found in the deployment tree. Pass a valid flow span id from ai-trace show output."
                raise ValueError(msg)
            selected_flow_ids = {selection.flow_span_id}
        elif selection.flow_name is not None:
            selected_flow_ids = {flow.span_id for flow in trace.flow_spans if flow.name == selection.flow_name}
            if not selected_flow_ids:
                available = sorted({flow.name for flow in trace.flow_spans})
                msg = (
                    f"No flow named {selection.flow_name!r} found. "
                    f"Available flow names: {', '.join(available) or 'none'}. "
                    f"Pass a valid --flow-name or --flow-span-id."
                )
                raise ValueError(msg)
        else:
            msg = "Flow inspection requires flow_name or flow_span_id. Pass --flow-name or --flow-span-id."
            raise ValueError(msg)
        selected_task_ids = {task_id for flow_id in selected_flow_ids for task_id in trace.tasks_by_flow.get(flow_id, ())}
        return selected_flow_ids, selected_task_ids

    if selection.task_span_id is None:
        msg = "Task inspection requires task_span_id. Pass --task-span-id with a valid task span UUID."
        raise ValueError(msg)
    task = trace.tasks.get(selection.task_span_id)
    if task is None:
        msg = f"Task span {selection.task_span_id} was not found in the deployment tree. Pass a valid task span id from ai-trace show output."
        raise ValueError(msg)
    selected_task_ids = _collect_all_descendant_task_ids(task, trace)
    selected_task_ids.update(_related_producer_task_ids(task, provenance))
    selected_task_ids.update(_related_consumer_task_ids(task, provenance))
    selected_flow_ids = {trace.tasks[task_id].parent_flow_span.span_id for task_id in selected_task_ids if task_id in trace.tasks}
    return selected_flow_ids, selected_task_ids


def _collect_all_descendant_task_ids(task: LoadedTask, trace: LoadedTrace) -> set[UUID]:
    """Recursively collect the target task and all its descendant task IDs."""
    result: set[UUID] = {task.span.span_id}
    stack = list(task.child_task_ids)
    while stack:
        child_id = stack.pop()
        if child_id in result:
            continue
        result.add(child_id)
        child_task = trace.tasks.get(child_id)
        if child_task is not None:
            stack.extend(child_task.child_task_ids)
    return result


def _related_producer_task_ids(task: LoadedTask, provenance: dict[str, ProvenanceEntry]) -> set[UUID]:
    producer_task_ids: set[UUID] = set()
    for document_sha in task.input_document_shas:
        entry = provenance.get(document_sha)
        if entry is not None and entry.produced_by_task_id is not None:
            producer_task_ids.add(entry.produced_by_task_id)
    return producer_task_ids


def _related_consumer_task_ids(task: LoadedTask, provenance: dict[str, ProvenanceEntry]) -> set[UUID]:
    consumer_task_ids: set[UUID] = set()
    for document_sha in task.output_document_shas:
        entry = provenance.get(document_sha)
        if entry is not None:
            consumer_task_ids.update(entry.consumed_by_task_ids)
    return consumer_task_ids


def _build_file_maps(
    trace: LoadedTrace,
    selected_flow_ids: set[UUID],
    selected_task_ids: set[UUID],
    batch_threshold: int,
) -> tuple[TaskFileMap, dict[UUID, str]]:
    task_file_map: TaskFileMap = {}
    flow_file_map: dict[UUID, str] = {}
    selected_flows = tuple(flow for flow in trace.flow_spans if flow.span_id in selected_flow_ids)
    flow_padding_width = max(2, len(str(len(selected_flows))))
    for flow_index, flow_span in enumerate(selected_flows, start=1):
        flow_dir_name = f"{flow_index:0{flow_padding_width}d}_{snake_case_segment(flow_span.name)}_F{flow_index:03d}"
        flow_root = f"flows/{flow_dir_name}"
        flow_file_map[flow_span.span_id] = f"{flow_root}/flow.md"
        task_ids = root_task_ids(trace, flow_span.span_id, selected_task_ids)
        _assign_task_paths(
            trace,
            task_file_map,
            task_ids,
            selected_task_ids=selected_task_ids,
            parent_tasks_dir=f"{flow_root}/tasks",
            batch_threshold=batch_threshold,
        )
    return task_file_map, flow_file_map


def _assign_task_paths(
    trace: LoadedTrace,
    task_file_map: TaskFileMap,
    task_ids: tuple[UUID, ...],
    *,
    selected_task_ids: set[UUID],
    parent_tasks_dir: str,
    batch_threshold: int,
) -> None:
    padding_width = max(2, len(str(len(task_ids))))
    task_groups = build_batch_groups(
        trace,
        task_ids,
        selected_task_ids=selected_task_ids,
        batch_threshold=batch_threshold,
    )
    for task_order, (_, grouped_task_ids) in enumerate(task_groups, start=1):
        task = trace.tasks[grouped_task_ids[0]]
        if len(grouped_task_ids) > 1:
            batch_dir_name = _batch_dir_name(trace, grouped_task_ids, task_order, padding_width)
            batch_path = f"{parent_tasks_dir}/{batch_dir_name}/batch.md"
            for grouped_task_id in grouped_task_ids:
                task_file_map[grouped_task_id] = batch_path
            continue
        task_dir_name = _task_dir_name(task, task_order, padding_width)
        task_root = f"{parent_tasks_dir}/{task_dir_name}"
        task_file_map[grouped_task_ids[0]] = f"{task_root}/task.md"  # pyright: ignore[reportGeneralTypeIssues] — grouped_task_ids is non-empty
        child_task_ids = tuple(child_id for child_id in task.child_task_ids if child_id in selected_task_ids)
        if child_task_ids:
            _assign_task_paths(
                trace,
                task_file_map,
                child_task_ids,
                selected_task_ids=selected_task_ids,
                parent_tasks_dir=f"{task_root}/tasks",
                batch_threshold=batch_threshold,
            )


def _selected_document_shas(trace: LoadedTrace, selected_task_ids: set[UUID]) -> set[str]:
    document_shas: set[str] = set(trace.root_span.input_document_shas)
    for task_id in selected_task_ids:
        task = trace.tasks[task_id]
        document_shas.update(task.input_document_shas)
        document_shas.update(task.output_document_shas)
    return {document_sha for document_sha in document_shas if document_sha in trace.documents}


async def _write_documents(
    docs_dir: Path,
    trace: LoadedTrace,
    provenance: dict[str, ProvenanceEntry],
    selected_document_shas: set[str],
    config: RenderConfig,
) -> None:
    await ensure_directory(docs_dir)
    for document_sha in sorted(selected_document_shas):
        document = trace.documents[document_sha]
        markdown = render_document_file(document, provenance.get(document_sha), config)
        await write_text(docs_dir / document.output_filename, markdown)


async def _write_flow_bundle(
    *,
    output_dir: Path,
    trace: LoadedTrace,
    provenance: dict[str, ProvenanceEntry],
    flow_id: UUID,
    selected_task_ids: set[UUID],
    task_file_map: TaskFileMap,
    flow_file_map: dict[UUID, str],
    selection: Selection,
) -> None:
    flow_markdown_path = output_dir / flow_file_map[flow_id]
    await ensure_directory(flow_markdown_path.parent / "tasks")
    flow_markdown = render_flow_markdown(trace, flow_id, task_file_map, selected_task_ids, selection.render_config.batch_threshold)
    await write_text(flow_markdown_path, flow_markdown)
    await _write_task_tree(
        output_dir=output_dir,
        trace=trace,
        provenance=provenance,
        task_file_map=task_file_map,
        flow_file_map=flow_file_map,
        selection=selection,
        task_ids=root_task_ids(trace, flow_id, selected_task_ids),
        selected_task_ids=selected_task_ids,
    )


async def _write_task_tree(
    *,
    output_dir: Path,
    trace: LoadedTrace,
    provenance: dict[str, ProvenanceEntry],
    task_file_map: TaskFileMap,
    flow_file_map: dict[UUID, str],
    selection: Selection,
    task_ids: tuple[UUID, ...],
    selected_task_ids: set[UUID],
) -> None:
    task_groups = build_batch_groups(
        trace,
        task_ids,
        selected_task_ids=selected_task_ids,
        batch_threshold=selection.render_config.batch_threshold,
    )
    for _, grouped_task_ids in task_groups:
        if len(grouped_task_ids) > 1:
            batch_markdown_path = output_dir / task_file_map[grouped_task_ids[0]]
            await ensure_directory(batch_markdown_path.parent)
            markdown = render_batch_markdown(
                trace,
                tuple(trace.tasks[grouped_task_id] for grouped_task_id in grouped_task_ids),
                provenance,
                task_file_map=task_file_map,
                flow_file_map=flow_file_map,
                config=selection.render_config,
            )
            await write_text(batch_markdown_path, markdown)
            continue
        task = trace.tasks[grouped_task_ids[0]]  # pyright: ignore[reportGeneralTypeIssues] — grouped_task_ids is non-empty
        task_markdown_path = output_dir / task_file_map[grouped_task_ids[0]]  # pyright: ignore[reportGeneralTypeIssues]
        await ensure_directory(task_markdown_path.parent)
        markdown = render_task_markdown(
            trace,
            task,
            provenance,
            task_file_map=task_file_map,
            flow_file_map=flow_file_map,
            config=selection.render_config,
        )
        await write_text(task_markdown_path, markdown)
        child_task_ids = tuple(child_id for child_id in task.child_task_ids if child_id in selected_task_ids)
        if child_task_ids:
            await _write_task_tree(
                output_dir=output_dir,
                trace=trace,
                provenance=provenance,
                task_file_map=task_file_map,
                flow_file_map=flow_file_map,
                selection=selection,
                task_ids=child_task_ids,
                selected_task_ids=selected_task_ids,
            )


def _task_dir_name(task: LoadedTask, task_order: int, padding_width: int) -> str:
    return f"{task_order:0{padding_width}d}_{snake_case_segment(task.span.name)}_T{task.span.sequence_no + 1:03d}"


def _batch_dir_name(trace: LoadedTrace, task_ids: tuple[UUID, ...], task_order: int, padding_width: int) -> str:
    first_task = trace.tasks[task_ids[0]]
    last_task = trace.tasks[task_ids[-1]]
    return (
        f"{task_order:0{padding_width}d}_{snake_case_segment(first_task.span.name)}_batch_"
        f"T{first_task.span.sequence_no + 1:03d}-T{last_task.span.sequence_no + 1:03d}"
    )
