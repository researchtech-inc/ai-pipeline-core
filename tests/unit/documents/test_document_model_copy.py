"""Tests for Document.model_copy, pickle, and copy — all blocked to prevent validation bypass."""

import copy
import pickle

import pytest

from ai_pipeline_core.documents import Document


class SampleFlowDoc(Document):
    """Sample flow document for testing."""


class TestModelCopyBlocked:
    """model_copy is blocked on Document to prevent validation and lifecycle bypass."""

    def test_model_copy_raises_type_error(self):
        doc = SampleFlowDoc.create_root(name="test.json", content={"data": "value"}, reason="test input")
        with pytest.raises(TypeError, match=r"model_copy.*not supported"):
            doc.model_copy()

    def test_model_copy_with_update_raises(self):
        doc = SampleFlowDoc.create_root(name="test.json", content={"data": "value"}, reason="test input")
        with pytest.raises(TypeError, match=r"model_copy.*not supported"):
            doc.model_copy(update={"name": "other.json"})

    def test_model_copy_deep_raises(self):
        doc = SampleFlowDoc.create_root(name="test.json", content={"data": "value"}, reason="test input")
        with pytest.raises(TypeError, match=r"model_copy.*not supported"):
            doc.model_copy(deep=True)

    def test_error_message_guides_to_derive(self):
        doc = SampleFlowDoc.create_root(name="test.json", content={"data": "value"}, reason="test input")
        with pytest.raises(TypeError, match=r"derive.*create"):
            doc.model_copy()


class TestPickleBlocked:
    """Pickle serialization is blocked — Documents must use JSON serialization."""

    def test_pickle_dumps_raises(self):
        doc = SampleFlowDoc.create_root(name="test.json", content={"data": "value"}, reason="test input")
        with pytest.raises(TypeError, match=r"pickle.*not supported"):
            pickle.dumps(doc)

    def test_pickle_dumps_protocol_2_raises(self):
        doc = SampleFlowDoc.create_root(name="test.json", content={"data": "value"}, reason="test input")
        with pytest.raises(TypeError, match=r"pickle.*not supported"):
            pickle.dumps(doc, protocol=2)

    def test_error_message_guides_to_json(self):
        doc = SampleFlowDoc.create_root(name="test.json", content={"data": "value"}, reason="test input")
        with pytest.raises(TypeError, match=r"model_dump.*model_validate"):
            pickle.dumps(doc)


class TestCopyBlocked:
    """copy.copy() and copy.deepcopy() are blocked — use derive() or create()."""

    def test_copy_raises(self):
        doc = SampleFlowDoc.create_root(name="test.json", content={"data": "value"}, reason="test input")
        with pytest.raises(TypeError, match=r"copying.*not supported"):
            copy.copy(doc)

    def test_deepcopy_raises(self):
        doc = SampleFlowDoc.create_root(name="test.json", content={"data": "value"}, reason="test input")
        with pytest.raises(TypeError, match=r"copying.*not supported"):
            copy.deepcopy(doc)

    def test_copy_error_guides_to_derive(self):
        doc = SampleFlowDoc.create_root(name="test.json", content={"data": "value"}, reason="test input")
        with pytest.raises(TypeError, match=r"derive.*create"):
            copy.copy(doc)
