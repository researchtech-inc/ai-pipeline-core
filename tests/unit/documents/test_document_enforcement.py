"""Tests for strict document creation enforcement.

These tests verify that documents can only be created within proper
pipeline task/flow context. They live in tests/documents/ (not tests/pipeline/)
because the pipeline conftest.py has an autouse fixture that suppresses
registration — which is exactly what these tests need to verify.
"""

import pytest

from ai_pipeline_core.documents import Document
from ai_pipeline_core.pipeline._execution_context import TaskContext, set_task_context


class _EnforcementDoc(Document):
    """Test document for enforcement tests."""


def test_create_root_outside_context_succeeds() -> None:
    doc = _EnforcementDoc.create_root(name="root.txt", content=b"root data", reason="deployment input")
    assert doc.name == "root.txt"
    assert doc.content == b"root data"


def test_create_root_requires_nonempty_reason() -> None:
    with pytest.raises(ValueError, match="requires a non-empty reason"):
        _EnforcementDoc.create_root(name="root.txt", content=b"data", reason="")


def test_create_requires_triggered_by() -> None:
    ctx = TaskContext(task_class_name="SomeTask")
    with set_task_context(ctx):
        with pytest.raises(TypeError, match="triggered_by"):
            _EnforcementDoc.create(name="test.txt", content="data")


def test_document_creation_inside_task_context_succeeds() -> None:
    source = _EnforcementDoc.create_root(name="source.txt", content=b"source", reason="test provenance")
    ctx = TaskContext(task_class_name="SomeTask")
    with set_task_context(ctx):
        doc = _EnforcementDoc.derive(
            name="task_output.txt",
            content="output data",
            derived_from=(source,),
        )
        assert doc.name == "task_output.txt"


def test_document_instantiate_base_class_raises() -> None:
    with pytest.raises(TypeError, match="Cannot instantiate Document directly"):
        Document(name="test.txt", content=b"data")
