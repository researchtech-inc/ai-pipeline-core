"""Tests for single-document PipelineTask return annotations."""

# pyright: reportPrivateUsage=false, reportUnusedClass=false

import pytest

from ai_pipeline_core import pipeline_test_context
from ai_pipeline_core.documents.document import Document
from ai_pipeline_core.pipeline._task import PipelineTask
from ai_pipeline_core.pipeline._type_validation import (
    _task_return_annotation_error,
    validate_task_return_annotation,
)


class SDInputDoc(Document):
    """Input document for single-doc return tests."""


class SDOutputDoc(Document):
    """Output document for single-doc return tests."""


class SDSingleReturnTask(PipelineTask):
    """Returns a single document."""

    @classmethod
    async def run(cls, input_docs: tuple[SDInputDoc, ...]) -> SDOutputDoc:
        _ = cls
        return SDOutputDoc.derive(derived_from=(input_docs[0],), name="out.md", content="x")


def test_single_document_annotation_is_accepted() -> None:
    """Bare Document subclasses are valid task return annotations."""
    assert _task_return_annotation_error(SDOutputDoc) is None


def test_validate_task_return_accepts_single_document() -> None:
    """validate_task_return_annotation accepts -> SDOutputDoc."""
    output_types = validate_task_return_annotation(SDOutputDoc, task_name="SingleDocTask")
    assert output_types == [SDOutputDoc]


def test_task_class_with_single_return_is_accepted() -> None:
    """PipelineTask with -> SDOutputDoc passes __init_subclass__ validation."""
    assert SDSingleReturnTask.output_document_types == (SDOutputDoc,)


@pytest.mark.asyncio
async def test_single_document_runtime_normalizes_to_tuple() -> None:
    """Runtime wraps a bare Document return into a 1-tuple."""
    source = SDInputDoc.create_root(name="in.md", content="x", reason="test")
    with pipeline_test_context():
        result = await SDSingleReturnTask.run((source,))
    assert isinstance(result, tuple)
    assert len(result) == 1
    assert isinstance(result[0], SDOutputDoc)
