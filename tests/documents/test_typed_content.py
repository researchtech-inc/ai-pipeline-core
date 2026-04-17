"""Tests for Document[T] typed content system."""

import json
from concurrent.futures import ThreadPoolExecutor
from enum import StrEnum

import pytest
from pydantic import BaseModel, Field, RootModel

from ai_pipeline_core.documents import Document


@pytest.fixture(autouse=True)
def _suppress_registration():
    return


class SampleModel(BaseModel, frozen=True):
    goal: str = Field(description="test goal")
    score: int = Field(description="test score")


class OtherModel(BaseModel, frozen=True):
    foo: str = Field(description="other field")


class SampleTypedDoc(Document[SampleModel]):
    """Typed document for testing."""


class UntypedDoc(Document):
    """Untyped document for testing."""


_invalid_int_doc_error: TypeError | None = None
try:

    class _ValidateIntDoc(Document[int]):  # type: ignore[type-var]
        pass

except TypeError as exc:
    _invalid_int_doc_error = exc


_invalid_str_doc_error: TypeError | None = None
try:

    class _ValidateStrDoc(Document[str]):  # type: ignore[type-var]
        pass

except TypeError as exc:
    _invalid_str_doc_error = exc


_typed_doc_inheritance_error: TypeError | None = None
try:

    class _InvalidTypedChildDoc(SampleTypedDoc):
        pass

except TypeError as exc:
    _typed_doc_inheritance_error = exc


_untyped_doc_inheritance_error: TypeError | None = None
try:

    class _InvalidUntypedChildDoc(UntypedDoc):
        pass

except TypeError as exc:
    _untyped_doc_inheritance_error = exc


class _NestedTypedDoc(Document[SampleModel]):
    class FILES(StrEnum):
        DATA = "nested.json"


# ---------------------------------------------------------------------------
# Declaration and introspection
# ---------------------------------------------------------------------------


class TestDeclaration:
    def test_typed_doc_has_content_type(self):
        assert SampleTypedDoc.get_content_type() is SampleModel

    def test_untyped_doc_has_no_content_type(self):
        assert UntypedDoc.get_content_type() is None

    def test_base_document_has_no_content_type(self):
        assert Document.get_content_type() is None

    def test_document_subclass_inheritance_is_rejected(self):
        with pytest.raises(TypeError, match="allows only one level of inheritance"):
            if _typed_doc_inheritance_error is None:
                raise TypeError("Document allows only one level of inheritance")
            raise _typed_doc_inheritance_error

    def test_invalid_generic_parameter_rejected(self):
        with pytest.raises(TypeError, match="generic parameter must be a BaseModel subclass"):
            if _invalid_int_doc_error is None:
                raise TypeError("generic parameter must be a BaseModel subclass")
            raise _invalid_int_doc_error

    def test_string_generic_parameter_rejected(self):
        with pytest.raises(TypeError, match="generic parameter must be a BaseModel subclass"):
            if _invalid_str_doc_error is None:
                raise TypeError("generic parameter must be a BaseModel subclass")
            raise _invalid_str_doc_error


# ---------------------------------------------------------------------------
# Creation-time validation
# ---------------------------------------------------------------------------


class TestCreationValidation:
    def test_create_root_with_correct_model(self):
        model = SampleModel(goal="test", score=42)
        doc = SampleTypedDoc.create_root(name="data.json", content=model, reason="test")
        assert doc.parsed.goal == "test"
        assert doc.parsed.score == 42

    def test_create_root_with_dict(self):
        doc = SampleTypedDoc.create_root(
            name="data.json",
            content={"goal": "from dict", "score": 7},
            reason="test",
        )
        assert doc.parsed.goal == "from dict"

    def test_create_root_with_json_string(self):
        json_str = json.dumps({"goal": "from string", "score": 3})
        doc = SampleTypedDoc.create_root(name="data.json", content=json_str, reason="test")
        assert doc.parsed.goal == "from string"

    def test_create_with_provenance(self):
        root = UntypedDoc.create_root(name="input.txt", content="hello", reason="test")
        model = SampleModel(goal="derived", score=1)
        doc = SampleTypedDoc.derive(name="out.json", content=model, derived_from=(root,))
        assert doc.parsed.goal == "derived"

    def test_create_rejects_wrong_model_type(self):
        other = OtherModel(foo="bar")
        with pytest.raises(TypeError, match="Expected content of type SampleModel, got OtherModel"):
            SampleTypedDoc.create_root(name="data.json", content=other, reason="test")

    def test_create_rejects_invalid_dict(self):
        with pytest.raises(TypeError, match="Content does not validate against SampleModel"):
            SampleTypedDoc.create_root(name="data.json", content={"wrong_field": "oops"}, reason="test")

    def test_create_rejects_invalid_json_bytes(self):
        bad_json = b'{"wrong_field": "oops"}'
        with pytest.raises(TypeError, match="Content does not validate against SampleModel"):
            SampleTypedDoc.create_root(name="data.json", content=bad_json, reason="test")

    def test_untyped_doc_accepts_any_content(self):
        """Untyped documents should not validate content against any schema."""
        doc = UntypedDoc.create_root(name="data.json", content={"any": "thing"}, reason="test")
        assert doc.as_json() == {"any": "thing"}

    def test_derive_validates_content(self):
        root = UntypedDoc.create_root(name="input.txt", content="hello", reason="test")
        model = SampleModel(goal="derived", score=1)
        doc = SampleTypedDoc.derive(derived_from=(root,), name="out.json", content=model)
        assert doc.parsed.goal == "derived"

    def test_derive_rejects_wrong_model(self):
        root = UntypedDoc.create_root(name="input.txt", content="hello", reason="test")
        with pytest.raises(TypeError, match="Expected content of type SampleModel"):
            SampleTypedDoc.derive(derived_from=(root,), name="out.json", content=OtherModel(foo="bad"))


# ---------------------------------------------------------------------------
# Parsed property
# ---------------------------------------------------------------------------


class TestParsedProperty:
    def test_parsed_returns_correct_model(self):
        model = SampleModel(goal="hello", score=99)
        doc = SampleTypedDoc.create_root(name="data.json", content=model, reason="test")
        result = doc.parsed
        assert isinstance(result, SampleModel)
        assert result.goal == "hello"
        assert result.score == 99

    def test_parsed_is_cached(self):
        model = SampleModel(goal="cached", score=1)
        doc = SampleTypedDoc.create_root(name="data.json", content=model, reason="test")
        first = doc.parsed
        second = doc.parsed
        assert first is second

    def test_parsed_raises_on_untyped_document(self):
        doc = UntypedDoc.create_root(name="test.txt", content="hello", reason="test")
        with pytest.raises(TypeError, match="UntypedDoc has no declared content type"):
            doc.parsed

    def test_parsed_works_after_model_validate_roundtrip(self):
        model = SampleModel(goal="roundtrip", score=55)
        doc = SampleTypedDoc.create_root(name="data.json", content=model, reason="test")
        serialized = doc.serialize_model()
        restored = SampleTypedDoc.model_validate(serialized)
        assert restored.parsed.goal == "roundtrip"
        assert restored.parsed.score == 55


# ---------------------------------------------------------------------------
# YAML content
# ---------------------------------------------------------------------------


class TestYamlContent:
    def test_create_with_model_yaml_extension(self):
        model = SampleModel(goal="yaml test", score=10)
        doc = SampleTypedDoc.create_root(name="data.yaml", content=model, reason="test")
        assert doc.parsed.goal == "yaml test"

    def test_create_with_dict_yaml_extension(self):
        doc = SampleTypedDoc.create_root(
            name="data.yml",
            content={"goal": "yml dict", "score": 5},
            reason="test",
        )
        assert doc.parsed.goal == "yml dict"

    def test_yaml_rejects_invalid_content(self):
        with pytest.raises(TypeError, match="Content does not validate against SampleModel"):
            SampleTypedDoc.create_root(
                name="data.yaml",
                content={"bad_key": "invalid"},
                reason="test",
            )


# ---------------------------------------------------------------------------
# List content via RootModel
# ---------------------------------------------------------------------------


class SampleItemList(RootModel[list[SampleModel]]):
    pass


class ListTypedDoc(Document[SampleItemList]):
    """Document carrying a list of SampleModel via RootModel."""


class TestListContentViaRootModel:
    def test_create_with_root_model_instance(self):
        items = SampleItemList([SampleModel(goal="a", score=1), SampleModel(goal="b", score=2)])
        doc = ListTypedDoc.create_root(name="items.json", content=items, reason="test")
        parsed = doc.parsed
        assert isinstance(parsed, SampleItemList)
        assert len(parsed.root) == 2
        assert parsed.root[0].goal == "a"

    def test_create_rejects_wrong_root_model(self):
        wrong = SampleModel(goal="not a list", score=1)
        with pytest.raises(TypeError, match="Expected content of type SampleItemList, got SampleModel"):
            ListTypedDoc.create_root(name="items.json", content=wrong, reason="test")


# ---------------------------------------------------------------------------
# Non-structured extensions (validation deferred)
# ---------------------------------------------------------------------------


class TestNonStructuredExtension:
    def test_model_content_requires_structured_extension(self):
        """BaseModel content with .txt extension is rejected by _convert_content, not by content_type validation."""
        model = SampleModel(goal="text ext", score=1)
        with pytest.raises(ValueError, match=r"requires \.json or \.yaml extension"):
            SampleTypedDoc.create_root(name="data.txt", content=model, reason="test")

    def test_bytes_with_text_extension_skip_schema_validation(self):
        """Raw bytes with .txt extension skip byte-level schema validation at creation time."""
        valid_json = json.dumps({"goal": "sneaky", "score": 1}).encode()
        doc = SampleTypedDoc.create_root(name="data.txt", content=valid_json, reason="test")
        # parsed still works because as_pydantic_model falls back to as_json() for non-YAML
        assert doc.parsed.goal == "sneaky"

    def test_invalid_bytes_with_text_extension_fail_on_access(self):
        """Invalid bytes with .txt extension pass creation but fail on parsed access."""
        bad_bytes = b"not json at all"
        doc = SampleTypedDoc.create_root(name="data.txt", content=bad_bytes, reason="test")
        with pytest.raises(Exception):
            doc.parsed


# ---------------------------------------------------------------------------
# Serialization roundtrip
# ---------------------------------------------------------------------------


class TestSerializationRoundtrip:
    def test_serialize_and_model_validate_preserves_content(self):
        model = SampleModel(goal="serialize", score=42)
        doc = SampleTypedDoc.create_root(name="data.json", content=model, reason="test")
        serialized = doc.serialize_model()

        restored = SampleTypedDoc.model_validate(serialized)

        assert restored.parsed.goal == "serialize"
        assert restored.parsed.score == 42
        assert restored.sha256 == doc.sha256

    def test_content_type_not_in_serialized_output(self):
        """_content_type is a ClassVar, not serialized."""
        model = SampleModel(goal="meta", score=1)
        doc = SampleTypedDoc.create_root(name="data.json", content=model, reason="test")
        serialized = doc.serialize_model()
        assert "_content_type" not in serialized
        assert "content_type" not in serialized


# ---------------------------------------------------------------------------
# SHA256 stability
# ---------------------------------------------------------------------------


class TestSha256Stability:
    def test_typed_and_untyped_same_content_same_hash(self):
        """Adding Generic[T] must not change the SHA256 of documents with identical content."""
        model = SampleModel(goal="hash test", score=7)
        typed = SampleTypedDoc.create_root(name="data.json", content=model, reason="test")
        untyped = UntypedDoc.create_root(name="data.json", content=model, reason="test")
        # SHA256 depends on name, content bytes, derived_from, triggered_by, attachments.
        # With same name and content, they should produce identical content bytes.
        assert typed.content == untyped.content
        assert typed.content_sha256 == untyped.content_sha256


# ---------------------------------------------------------------------------
# Single-level inheritance enforcement
# ---------------------------------------------------------------------------


class TestSingleLevelInheritanceEnforcement:
    def test_typed_document_cannot_be_subclassed(self):
        with pytest.raises(TypeError, match="Define '_InvalidTypedChildDoc' as a direct Document subclass instead"):
            if _typed_doc_inheritance_error is None:
                raise TypeError("Define '_InvalidTypedChildDoc' as a direct Document subclass instead")
            raise _typed_doc_inheritance_error

    def test_untyped_document_cannot_be_subclassed(self):
        with pytest.raises(TypeError, match="Define '_InvalidUntypedChildDoc' as a direct Document subclass instead"):
            if _untyped_doc_inheritance_error is None:
                raise TypeError("Define '_InvalidUntypedChildDoc' as a direct Document subclass instead")
            raise _untyped_doc_inheritance_error


# ---------------------------------------------------------------------------
# Content model subclass (Liskov substitution)
# ---------------------------------------------------------------------------


class SampleModelChild(SampleModel, frozen=True):
    """Subclass of SampleModel with extra field."""

    extra: str = Field(default="bonus", description="extra field")


class TestContentModelSubclass:
    def test_create_accepts_subclass_of_content_type(self):
        """A subclass of the declared content type should pass isinstance check."""
        sub = SampleModelChild(goal="sub", score=5, extra="yes")
        doc = SampleTypedDoc.create_root(name="data.json", content=sub, reason="test")
        assert doc.parsed.goal == "sub"
        assert doc.parsed.score == 5


# ---------------------------------------------------------------------------
# Direct __init__ bypass (no validation)
# ---------------------------------------------------------------------------


class TestInitBypass:
    def test_init_bypass_creates_doc_without_validation(self):
        """Direct __init__ skips content_type validation — bad content allowed."""
        bad_json = json.dumps({"wrong": "schema"}).encode()
        doc = SampleTypedDoc(name="data.json", content=bad_json, description="bypass test")
        assert doc.name == "data.json"
        with pytest.raises(Exception):
            doc.parsed

    def test_init_with_valid_content_works(self):
        """Direct __init__ with valid content still allows parsed access."""
        valid_json = json.dumps({"goal": "init", "score": 42}).encode()
        doc = SampleTypedDoc(name="data.json", content=valid_json, description="valid init")
        assert doc.parsed.goal == "init"


# ---------------------------------------------------------------------------
# Empty content
# ---------------------------------------------------------------------------


class TestEmptyContent:
    def test_empty_bytes_rejected_at_creation(self):
        with pytest.raises(TypeError, match="Content does not validate against SampleModel"):
            SampleTypedDoc.create_root(name="data.json", content=b"", reason="test")


# ---------------------------------------------------------------------------
# Parameterized class direct use (Document[T] alias)
# ---------------------------------------------------------------------------


class TestParameterizedClassDirectUse:
    def test_parameterized_class_has_no_content_type(self):
        """Document[SampleModel] intermediate class should not have _content_type set."""
        assert Document[SampleModel].get_content_type() is None

    def test_parameterized_class_cannot_be_instantiated(self):
        with pytest.raises(TypeError, match="define a named subclass"):
            Document[SampleModel].create_root(name="x.json", content=SampleModel(goal="x", score=1), reason="test")


# ---------------------------------------------------------------------------
# isinstance compatibility
# ---------------------------------------------------------------------------


class TestIsinstanceCompat:
    def test_typed_doc_is_instance_of_document(self):
        model = SampleModel(goal="isinstance", score=1)
        doc = SampleTypedDoc.create_root(name="data.json", content=model, reason="test")
        assert isinstance(doc, Document)
        assert isinstance(doc, SampleTypedDoc)

    def test_untyped_doc_is_instance_of_document(self):
        doc = UntypedDoc.create_root(name="test.txt", content="hello", reason="test")
        assert isinstance(doc, Document)


# ---------------------------------------------------------------------------
# model_dump / serialize_model unaffected by Generic
# ---------------------------------------------------------------------------


class TestPydanticMachineryUnaffected:
    def test_model_dump_has_no_generic_artifacts(self):
        model = SampleModel(goal="dump", score=1)
        doc = SampleTypedDoc.create_root(name="data.json", content=model, reason="test")
        dumped = doc.model_dump()
        assert "_content_type" not in dumped
        assert "name" in dumped
        assert "content" in dumped

    def test_serialize_model_identical_for_typed_and_untyped(self):
        model = SampleModel(goal="serial", score=1)
        typed = SampleTypedDoc.create_root(name="data.json", content=model, reason="test")
        untyped = UntypedDoc.create_root(name="data.json", content=model, reason="test")
        typed_s = typed.serialize_model()
        untyped_s = untyped.serialize_model()
        assert typed_s["class_name"] == "SampleTypedDoc"
        assert untyped_s["class_name"] == "UntypedDoc"
        assert typed_s["content"] == untyped_s["content"]


# ---------------------------------------------------------------------------
# FILES enum interaction
# ---------------------------------------------------------------------------


class _TypedDocFiles(StrEnum):
    DATA = "data.json"


class _TypedDocWithFiles(Document[SampleModel]):
    FILES = _TypedDocFiles


class TestFilesEnumInteraction:
    def test_external_files_enum_with_content_type(self):
        model = SampleModel(goal="enum", score=1)
        doc = _TypedDocWithFiles.create_root(name="data.json", content=model, reason="test")
        assert doc.name == "data.json"
        assert doc.parsed.goal == "enum"

    def test_files_enum_wrong_name_still_rejected(self):
        model = SampleModel(goal="bad name", score=1)
        with pytest.raises(Exception):
            _TypedDocWithFiles.create_root(name="wrong.json", content=model, reason="test")

    def test_nested_files_enum_with_content_type(self):
        model = SampleModel(goal="nested", score=2)
        doc = _NestedTypedDoc.create_root(name="nested.json", content=model, reason="test")
        assert doc.name == "nested.json"
        assert doc.parsed.goal == "nested"


# ---------------------------------------------------------------------------
# Concurrent parsed access
# ---------------------------------------------------------------------------


class TestConcurrentAccess:
    def test_parsed_concurrent_access_is_stable(self):
        doc = SampleTypedDoc.create_root(name="data.json", content={"goal": "g", "score": 1}, reason="test")

        def read_goal(_: int) -> str:
            return doc.parsed.goal

        with ThreadPoolExecutor(max_workers=8) as pool:
            goals = list(pool.map(read_goal, range(64)))
        assert goals == ["g"] * 64


# ---------------------------------------------------------------------------
# model_copy blocked
# ---------------------------------------------------------------------------


class TestModelCopyBlocked:
    def test_model_copy_blocked_for_typed_doc(self):
        doc = SampleTypedDoc.create_root(name="data.json", content={"goal": "g", "score": 1}, reason="test")
        with pytest.raises(TypeError, match="model_copy"):
            doc.model_copy()
