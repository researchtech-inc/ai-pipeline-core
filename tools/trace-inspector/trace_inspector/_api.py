"""Programmatic entry points for trace-inspector bundle generation."""

from trace_inspector._connect import open_reader_from_source
from trace_inspector._load import load_trace
from trace_inspector._provenance import build_provenance
from trace_inspector._types import Selection
from trace_inspector._write import write_full_bundle

__all__ = [
    "inspect_run",
]


async def inspect_run(selection: Selection) -> None:
    """Load a run/flow/task selection and write a markdown inspection bundle."""
    connection = await open_reader_from_source(selection.source)
    try:
        trace = await load_trace(connection.reader, connection.deployment_id, reader_label=connection.reader_label)
        provenance = build_provenance(trace)
        await write_full_bundle(trace, provenance, selection)
    finally:
        await connection.reader.shutdown()
