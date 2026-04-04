"""Comparison bundle orchestration for trace-inspector."""

from trace_inspector._connect import open_reader_from_source
from trace_inspector._helpers import prepare_output_dir, write_text
from trace_inspector._load import load_trace
from trace_inspector._render import render_comparison_markdown
from trace_inspector._types import ComparisonSelection, snake_case_segment

__all__ = [
    "compare_runs",
]


async def compare_runs(selection: ComparisonSelection) -> None:
    """Load both traces and write a task comparison bundle."""
    left_connection = await open_reader_from_source(selection.left_source)
    try:
        right_connection = await open_reader_from_source(selection.right_source)
        try:
            left_trace = await load_trace(left_connection.reader, left_connection.deployment_id, reader_label=left_connection.reader_label)
            right_trace = await load_trace(right_connection.reader, right_connection.deployment_id, reader_label=right_connection.reader_label)
            left_task = left_trace.tasks.get(selection.left_task_span_id)
            if left_task is None:
                msg = f"Task span {selection.left_task_span_id} was not found in the left trace. Pass a valid task span id from ai-trace show output."
                raise ValueError(msg)
            right_task = None
            if selection.right_task_span_id is not None:
                right_task = right_trace.tasks.get(selection.right_task_span_id)
            if right_task is None:
                matching_tasks = [task for task in right_trace.tasks.values() if task.span.name == left_task.span.name]
                if len(matching_tasks) > 1:
                    msg = (
                        f"Right trace contains {len(matching_tasks)} tasks named {left_task.span.name!r}. "
                        "Pass --right-task-span-id to choose the exact replay task instead of relying on ambiguous name matching."
                    )
                    raise ValueError(msg)
                right_task = matching_tasks[0] if matching_tasks else None
            if right_task is None:
                msg = (
                    f"Could not find a replay task matching {left_task.span.name!r} in the right trace. "
                    "Pass --right-task-span-id to select the replay task explicitly."
                )
                raise ValueError(msg)

            await prepare_output_dir(selection.output_dir)
            compare_dir = selection.output_dir / "compare"
            compare_dir.mkdir(parents=True, exist_ok=True)
            safe_name = snake_case_segment(left_task.span.name)
            markdown = render_comparison_markdown(left_task, right_task, left_trace, right_trace, selection.render_config)
            await write_text(compare_dir / f"{safe_name}.md", markdown)
            await write_text(
                compare_dir / "index.md",
                _build_compare_index(left_task.span.name, safe_name),
            )
        finally:
            await right_connection.reader.shutdown()
    finally:
        await left_connection.reader.shutdown()


def _build_compare_index(task_name: str, safe_name: str) -> str:
    return f"# Comparison\n\n- Task: `{task_name}`\n- File: [{safe_name}.md]({safe_name}.md)\n"
