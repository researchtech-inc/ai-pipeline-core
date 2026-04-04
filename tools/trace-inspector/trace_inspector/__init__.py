"""Programmatic API for trace-inspector."""

from trace_inspector._api import inspect_run
from trace_inspector._compare import compare_runs
from trace_inspector._types import ComparisonSelection, LoadedTrace, RenderConfig, Selection

__all__ = [
    "ComparisonSelection",
    "LoadedTrace",
    "RenderConfig",
    "Selection",
    "compare_runs",
    "inspect_run",
]
