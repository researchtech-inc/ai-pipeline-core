"""Core Document class tests."""

import base64
from enum import StrEnum

import pytest

from ai_pipeline_core.documents import Document
from ai_pipeline_core.documents.attachment import Attachment
from ai_pipeline_core.exceptions import DocumentNameError, DocumentSizeError


class ConcreteTestDocument(Document):
    """Concrete Document for testing."""

    def get_type(self) -> str:
        return "test_flow"


class ConcreteTestTaskDoc(Document):
    """Concrete Document for testing."""

    def get_type(self) -> str:
        return "test_task"


class _ConcreteFlowDoc(Document):
    """Concrete Document subclass for concrete instantiation tests."""

    def get_type(self) -> str:
        return "concrete_flow"


class _ConcreteTaskDoc(Document):
    """Concrete Document subclass for concrete instantiation tests."""

    def get_type(self) -> str:
        return "concrete_task"


class AllowedDocumentNames(StrEnum):
    ALLOWED_FILE_1 = "allowed1.txt"
    ALLOWED_FILE_2 = "allowed2.json"


class RestrictedDocument(Document):
    """Document with restricted file names."""

    FILES = AllowedDocumentNames

    def get_type(self) -> str:
        return "restricted"


class SmallDocument(Document):
    """Document with small size limit."""

    MAX_CONTENT_SIZE = 10

    def get_type(self) -> str:
        return "small"


@pytest.fixture(autouse=True)
def _suppress_registration():
    return


class TestDocumentValidation:
    """Test document validation logic."""

    def test_name_validation_with_files_enum(self):
        """Test name validation against FILES enum."""
        # Valid names should work
        doc = RestrictedDocument(
            name="allowed1.txt",
            content=b"test",
        )
        assert doc.name == "allowed1.txt"

        # Invalid name should raise
        with pytest.raises(DocumentNameError) as exc_info:
            RestrictedDocument(
                name="not_allowed.txt",
                content=b"test",
            )
        assert "Invalid filename" in str(exc_info.value)
        assert "allowed1.txt" in str(exc_info.value)

    def test_content_size_limit(self):
        """Test content size limit enforcement."""
        # Within limit should work
        doc = SmallDocument(
            name="test.txt",
            content=b"123456789",  # 9 bytes
        )
        assert doc.size == 9

        # Exceeding limit should raise
        with pytest.raises(DocumentSizeError) as exc_info:
            SmallDocument(
                name="test.txt",
                content=b"12345678901",  # 11 bytes
            )
        assert "exceeds maximum allowed size" in str(exc_info.value)

    def test_name_security_validation(self):
        """Test security validation for document names."""
        # Path traversal attempts should fail
        with pytest.raises(DocumentNameError):
            ConcreteTestDocument(name="../etc/passwd", content=b"test")

        with pytest.raises(DocumentNameError):
            ConcreteTestDocument(name="..\\windows\\system32", content=b"test")

        with pytest.raises(DocumentNameError):
            ConcreteTestDocument(name="/etc/passwd", content=b"test")

        # .meta.json extension should be rejected (reserved for store metadata)
        with pytest.raises(DocumentNameError) as exc_info:
            ConcreteTestDocument(name="test.meta.json", content=b"test")
        assert ".meta.json" in str(exc_info.value)

        # Empty or whitespace names should fail
        with pytest.raises(DocumentNameError):
            ConcreteTestDocument(name="", content=b"test")

        with pytest.raises(DocumentNameError):
            ConcreteTestDocument(name=" ", content=b"test")

        with pytest.raises(DocumentNameError):
            ConcreteTestDocument(name="test ", content=b"test")


class TestAbstractInstantiation:
    """Test that abstract document classes cannot be instantiated directly."""

    def test_cannot_instantiate_document(self):
        """Test that Document cannot be instantiated directly."""
        with pytest.raises(TypeError, match="Cannot instantiate Document directly"):
            Document(name="test.txt", content=b"test")

    def test_concrete_subclass_works(self):
        """Test that concrete Document subclasses can be instantiated."""
        doc = ConcreteTestDocument(name="test.txt", content=b"test")
        assert doc.name == "test.txt"

    def test_can_instantiate_concrete_subclasses(self):
        """Test that concrete subclasses can be instantiated."""
        doc = _ConcreteFlowDoc(name="test.txt", content=b"test")
        assert doc.name == "test.txt"
        assert doc.content == b"test"

        doc = _ConcreteTaskDoc(name="test.txt", content=b"test")
        assert doc.name == "test.txt"
        assert doc.content == b"test"


class _VisibleTestDoc(Document):
    """Document subclass used to test public visibility overrides."""

    publicly_visible = True


class TestPubliclyVisibleClassVar:
    def test_default_is_false(self):
        assert ConcreteTestDocument.publicly_visible is False

    def test_subclass_override_to_true(self):
        assert _VisibleTestDoc.publicly_visible is True
        # Base class unaffected
        assert ConcreteTestDocument.publicly_visible is False

    def test_not_a_model_field(self):
        """publicly_visible is a ClassVar, not a Pydantic field."""
        assert "publicly_visible" not in ConcreteTestDocument.model_fields

    def test_rejects_document_subclass_inheritance(self):
        """Document subclasses cannot be further subclassed."""
        with pytest.raises(TypeError, match="only one level of inheritance"):

            class _DerivedTestDoc(ConcreteTestDocument):
                """Should be rejected."""


class TestDocumentProperties:
    """Test document properties and methods."""

    def test_id_determinism(self):
        """Test that document ID is deterministic based on name + content."""
        content1 = b"Hello, world!"
        content2 = b"Different content"

        # Same name + content -> same ID
        doc1 = ConcreteTestDocument(name="test.txt", content=content1)
        doc2 = ConcreteTestDocument(name="test.txt", content=content1)
        assert doc1.id == doc2.id
        assert doc1.sha256 == doc2.sha256

        # Different name -> different ID (name is part of hash)
        doc3 = ConcreteTestDocument(name="other.txt", content=content1)
        assert doc3.sha256 != doc1.sha256

        # Different content -> different ID
        doc4 = ConcreteTestDocument(name="test.txt", content=content2)
        assert doc4.id != doc1.id
        assert doc4.sha256 != doc1.sha256

        # ID should be first 6 chars of SHA256
        assert len(doc1.id) == 6
        assert doc1.id == doc1.sha256[:6]

    def test_mime_detection_text(self):
        """Test MIME type detection for text documents."""
        # Plain text
        doc = ConcreteTestDocument(name="test.txt", content=b"Hello, world!")
        assert doc.is_text is True
        assert doc.is_image is False
        assert doc.is_pdf is False
        assert "text" in doc.mime_type

        # Markdown
        doc_md = ConcreteTestDocument(name="test.md", content=b"# Header\n\nContent")
        assert doc_md.is_text is True
        assert doc_md.mime_type == "text/markdown"

        # JSON
        doc_json = ConcreteTestDocument(name="test.json", content=b'{"key": "value"}')
        assert doc_json.is_text is True

    def test_mime_detection_binary(self):
        """Test MIME type detection for binary documents."""
        # Simple PNG header (minimal valid PNG)
        png_header = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde"
        doc_png = ConcreteTestDocument(name="test.png", content=png_header)
        assert doc_png.is_image is True
        assert doc_png.is_text is False
        assert doc_png.is_pdf is False
        assert "image" in doc_png.mime_type

        # PDF header
        pdf_header = b"%PDF-1.4\n%\xd3\xeb\xe9\xe1\n1 0 obj\n<</Type/Catalog>>\nendobj"
        doc_pdf = ConcreteTestDocument(name="test.pdf", content=pdf_header)
        assert doc_pdf.is_pdf is True
        assert doc_pdf.is_text is False
        assert doc_pdf.is_image is False
        assert "pdf" in doc_pdf.mime_type

    def test_text_property(self):
        """Test text property for text documents."""
        text_content = "Hello, 世界! 🌍"
        doc = ConcreteTestDocument(name="test.txt", content=text_content.encode("utf-8"))

        assert doc.text == text_content

        # Binary document should raise
        doc_binary = ConcreteTestDocument(name="test.bin", content=b"\x00\x01\x02\x03")
        with pytest.raises(ValueError) as exc_info:
            doc_binary.text
        assert "not text" in str(exc_info.value)

    def test_as_json_method(self):
        """Test as_json() method."""
        json_data = {"key": "value", "number": 123}
        import json

        doc = ConcreteTestDocument(name="test.json", content=json.dumps(json_data).encode())

        assert doc.as_json() == json_data

        # Invalid JSON should raise
        doc_invalid = ConcreteTestDocument(name="test.txt", content=b"not json")
        with pytest.raises(json.JSONDecodeError):
            doc_invalid.as_json()

    def test_as_yaml_method(self):
        """Test as_yaml() method."""
        yaml_content = "key: value\nnumber: 123\n"
        doc = ConcreteTestDocument(name="test.yaml", content=yaml_content.encode())

        result = doc.as_yaml()
        assert result["key"] == "value"
        assert result["number"] == 123

    def test_serialize_model(self):
        """Test serialize_model method."""
        doc = ConcreteTestDocument(name="test.txt", content=b"Hello, world!", description="Test document")

        serialized = doc.serialize_model()

        assert serialized["name"] == "test.txt"
        assert serialized["description"] == "Test document"
        assert "class_name" in serialized
        assert serialized["size"] == 13
        assert serialized["id"] == doc.id
        assert serialized["sha256"] == doc.sha256
        assert "mime_type" in serialized
        # Text content serialized as plain string
        assert serialized["content"] == "Hello, world!"

        # Binary content should be data URI
        binary_content = b"\x89PNG\r\n\x1a\n"  # PNG header - contains invalid UTF-8 sequences
        doc_binary = ConcreteTestDocument(name="test.bin", content=binary_content)
        serialized_binary = doc_binary.serialize_model()
        expected_b64 = base64.b64encode(binary_content).decode()
        assert serialized_binary["content"] == f"data:{doc_binary.mime_type};base64,{expected_b64}"

    def test_model_validate(self):
        """Test model_validate deserialization."""
        # Plain string content (text)
        data = {
            "name": "test.txt",
            "content": "Hello, world!",
            "description": "Test doc",
        }
        doc = ConcreteTestDocument.model_validate(data)
        assert doc.name == "test.txt"
        assert doc.content == b"Hello, world!"
        assert doc.description == "Test doc"

        # Data URI content (binary)
        b64 = base64.b64encode(b"\x00\x01\x02\x03").decode()
        data_binary = {
            "name": "test.bin",
            "content": f"data:application/octet-stream;base64,{b64}",
        }
        doc_binary = ConcreteTestDocument.model_validate(data_binary)
        assert doc_binary.content == b"\x00\x01\x02\x03"

    def test_document_type_name(self):
        """Test document class name is accessible."""
        flow_doc = ConcreteTestDocument(name="test.txt", content=b"test")
        assert type(flow_doc).__name__ == "ConcreteTestDocument"

        task_doc = ConcreteTestTaskDoc(name="test.txt", content=b"test")
        assert type(task_doc).__name__ == "ConcreteTestTaskDoc"


class TestDocumentConstructor:
    """Tests for the Document constructor."""

    def test_constructor_with_str_content(self):
        text = "Hello ✨"
        doc = ConcreteTestDocument.create_root(
            name="greeting.txt",
            description="str content",
            content=text,
            reason="test input",
        )
        assert isinstance(doc, ConcreteTestDocument)
        assert doc.name == "greeting.txt"
        assert doc.description == "str content"
        assert doc.content == text.encode("utf-8")
        assert doc.size == len(text.encode("utf-8"))
        # Deterministic hashing
        assert doc.id == doc.sha256[:6]

    def test_constructor_with_bytes_content(self):
        raw = b"\x00\xffdata"
        doc = ConcreteTestDocument(
            name="raw.bin",
            description=None,
            content=raw,
        )
        assert doc.content == raw
        assert doc.size == len(raw)
        assert not doc.is_text

    def test_constructor_validation_applies(self):
        # Name validation still applies through constructor
        with pytest.raises(DocumentNameError):
            ConcreteTestDocument.create_root(name="../hack.txt", description=None, content="x", reason="test input")

        # Size validation applies too (using SmallDocument)
        doc_ok = SmallDocument.create_root(name="ok.txt", description=None, content="12345", reason="test input")
        assert doc_ok.size == 5
        with pytest.raises(DocumentSizeError):
            SmallDocument.create_root(name="big.txt", description=None, content="12345678901", reason="test input")


class TestNewDocumentMethods:
    """Tests for Document methods like as_pydantic_model."""

    def test_as_pydantic_model_from_json(self):
        """Test parsing JSON document as Pydantic model."""
        from pydantic import BaseModel, Field

        class TestModel(BaseModel):
            name: str
            age: int = Field(ge=0)
            tags: list[str] = []

        # Create a JSON document
        json_content = '{"name": "Alice", "age": 30, "tags": ["python", "ai"]}'
        doc = ConcreteTestDocument(name="data.json", content=json_content.encode())

        # Parse as Pydantic model
        model = doc.as_pydantic_model(TestModel)
        assert isinstance(model, TestModel)
        assert model.name == "Alice"
        assert model.age == 30
        assert model.tags == ["python", "ai"]

        # Test with missing optional field
        json_content2 = '{"name": "Bob", "age": 25}'
        doc2 = ConcreteTestDocument(name="data2.json", content=json_content2.encode())
        model2 = doc2.as_pydantic_model(TestModel)
        assert model2.name == "Bob"
        assert model2.age == 25
        assert model2.tags == []

        # Test validation error
        json_invalid = '{"name": "Charlie", "age": -5}'
        doc_invalid = ConcreteTestDocument(name="invalid.json", content=json_invalid.encode())
        with pytest.raises(ValueError):  # Pydantic validation error
            doc_invalid.as_pydantic_model(TestModel)

    def test_as_pydantic_model_from_yaml(self):
        """Test parsing YAML document as Pydantic model."""
        from pydantic import BaseModel

        class ConfigModel(BaseModel):
            host: str
            port: int
            debug: bool = False

        # Create a YAML document
        yaml_content = """host: localhost
port: 8080
debug: true
"""
        doc = ConcreteTestDocument(name="config.yaml", content=yaml_content.encode())

        # Parse as Pydantic model
        model = doc.as_pydantic_model(ConfigModel)
        assert isinstance(model, ConfigModel)
        assert model.host == "localhost"
        assert model.port == 8080
        assert model.debug is True

        # Test with .yml extension
        doc_yml = ConcreteTestDocument(name="config.yml", content=yaml_content.encode())
        model_yml = doc_yml.as_pydantic_model(ConfigModel)
        assert model_yml.host == "localhost"

    def test_create_json_with_dict(self):
        """Test creating JSON document with dictionary data."""
        data = {"key": "value", "number": 42, "list": [1, 2, 3]}
        import json

        doc = ConcreteTestDocument(
            name="test.json",
            description="JSON from dict",
            content=json.dumps(data, indent=2).encode(),
        )

        assert doc.name == "test.json"
        assert doc.description == "JSON from dict"
        assert doc.is_text is True
        assert "json" in doc.mime_type

        # Verify content can be parsed back
        parsed = doc.as_json()
        assert parsed == data

        # Verify formatting (should be indented)
        text = doc.text
        assert "  " in text  # Check for indentation

    def test_create_json_with_pydantic_model(self):
        """Test creating JSON document with Pydantic model."""
        import json

        from pydantic import BaseModel

        class UserModel(BaseModel):
            username: str
            email: str
            active: bool = True

        user = UserModel(username="alice", email="alice@example.com")
        doc = ConcreteTestDocument(
            name="user.json",
            description="User data",
            content=json.dumps(user.model_dump(mode="json"), indent=2).encode(),
        )

        assert doc.name == "user.json"
        parsed = doc.as_json()
        assert parsed["username"] == "alice"
        assert parsed["email"] == "alice@example.com"
        assert parsed["active"] is True

    def test_json_document_creation(self):
        """Test creating JSON documents."""
        import json

        # Create JSON document directly
        data = {"key": "value"}
        doc = ConcreteTestDocument(
            name="test.json",
            description=None,
            content=json.dumps(data).encode(),
        )
        assert doc.as_json() == data

    def test_create_yaml_with_dict(self):
        """Test creating YAML document with dictionary data."""
        from io import BytesIO

        from ruamel.yaml import YAML

        data = {"database": {"host": "localhost", "port": 5432}, "cache": {"ttl": 300}}
        yaml = YAML()
        stream = BytesIO()
        yaml.dump(data, stream)
        doc = ConcreteTestDocument(name="config.yaml", description="YAML config", content=stream.getvalue())

        assert doc.name == "config.yaml"
        assert doc.description == "YAML config"
        assert doc.is_text is True
        assert "yaml" in doc.mime_type

        # Verify content can be parsed back
        parsed = doc.as_yaml()
        assert parsed == data

        # Verify YAML formatting
        text = doc.text
        assert "database:" in text
        assert "  host:" in text  # Check for indentation

    def test_create_yaml_with_yml_extension(self):
        """Test creating YAML document with .yml extension."""
        from io import BytesIO

        from ruamel.yaml import YAML

        data = {"test": "value"}
        yaml = YAML()
        stream = BytesIO()
        yaml.dump(data, stream)
        doc = ConcreteTestDocument(name="config.yml", description=None, content=stream.getvalue())
        assert doc.name == "config.yml"
        assert doc.as_yaml() == data

    def test_create_yaml_with_pydantic_model(self):
        """Test creating YAML document with Pydantic model."""
        from io import BytesIO

        from pydantic import BaseModel
        from ruamel.yaml import YAML

        class ServerConfig(BaseModel):
            name: str
            workers: int
            ssl_enabled: bool

        config = ServerConfig(name="api-server", workers=4, ssl_enabled=True)
        yaml = YAML()
        stream = BytesIO()
        yaml.dump(config.model_dump(mode="json"), stream)
        doc = ConcreteTestDocument(name="server.yaml", description="Server config", content=stream.getvalue())

        assert doc.name == "server.yaml"
        parsed = doc.as_yaml()
        assert parsed["name"] == "api-server"
        assert parsed["workers"] == 4
        assert parsed["ssl_enabled"] is True

    def test_json_yaml_roundtrip(self):
        """Test that JSON/YAML parsing roundtrips correctly."""
        import json
        from io import BytesIO

        from pydantic import BaseModel
        from ruamel.yaml import YAML

        class DataModel(BaseModel):
            items: list[str]
            metadata: dict[str, int]

        original = DataModel(items=["a", "b", "c"], metadata={"count": 3, "version": 1})

        # JSON roundtrip
        json_doc = ConcreteTestDocument(
            name="data.json",
            description=None,
            content=json.dumps(original.model_dump(mode="json"), indent=2).encode(),
        )
        json_model = json_doc.as_pydantic_model(DataModel)
        assert json_model == original

        # YAML roundtrip
        yaml = YAML()
        stream = BytesIO()
        yaml.dump(original.model_dump(mode="json"), stream)
        yaml_doc = ConcreteTestDocument(name="data.yaml", description=None, content=stream.getvalue())
        yaml_model = yaml_doc.as_pydantic_model(DataModel)
        assert yaml_model == original


class TestParsedMethod:
    """Tests for the parsed() method that reverses document creation."""

    def test_parsed_str_roundtrip(self):
        """Test that str content roundtrips correctly."""
        original = "Hello, World! 🌍"
        doc = ConcreteTestDocument.create_root(name="test.txt", content=original, reason="test input")
        assert doc.parse(str) == original

    def test_parsed_bytes_roundtrip(self):
        """Test that bytes content roundtrips correctly."""
        original = b"Binary \x00\xff\xfe data"
        doc = ConcreteTestDocument(name="data.bin", content=original)
        assert doc.parse(bytes) == original

    def test_parsed_json_dict_roundtrip(self):
        """Test that dict → JSON → dict roundtrips correctly."""
        import json

        original = {"name": "test", "values": [1, 2, 3], "nested": {"key": "value"}}
        doc = ConcreteTestDocument(
            name="data.json",
            content=json.dumps(original, indent=2).encode(),
        )
        assert doc.parse(dict) == original

    def test_parsed_json_list_roundtrip(self):
        """Test that list → JSON → list roundtrips correctly."""
        import json

        original = [1, 2, {"key": "value"}, "text"]
        doc = ConcreteTestDocument(name="array.json", content=json.dumps(original).encode())
        assert doc.parse(list) == original

    def test_parsed_yaml_dict_roundtrip(self):
        """Test that dict → YAML → dict roundtrips correctly."""
        from io import BytesIO

        from ruamel.yaml import YAML

        original = {"database": {"host": "localhost", "port": 5432}}
        yaml = YAML()
        stream = BytesIO()
        yaml.dump(original, stream)

        doc = ConcreteTestDocument(name="config.yaml", content=stream.getvalue())
        assert doc.parse(dict) == original

    def test_parsed_yaml_with_yml_extension(self):
        """Test that .yml extension works with parsed()."""
        from io import BytesIO

        from ruamel.yaml import YAML

        original = {"test": "value"}
        yaml = YAML()
        stream = BytesIO()
        yaml.dump(original, stream)

        doc = ConcreteTestDocument(name="config.yml", content=stream.getvalue())
        assert doc.parse(dict) == original

    def test_parsed_json_string_list_roundtrip(self):
        """Test that string list → JSON → list roundtrips correctly via create()."""
        original = ["First item", "Second item", "Third item"]
        doc = ConcreteTestDocument.create_root(name="items.json", content=original, reason="test input")
        assert doc.parse(list) == original

    def test_parsed_pydantic_model_json(self):
        """Test parsing JSON to Pydantic model."""
        import json

        from pydantic import BaseModel

        class UserModel(BaseModel):
            name: str
            age: int
            active: bool = True

        original = UserModel(name="Alice", age=30)
        doc = ConcreteTestDocument(
            name="user.json",
            content=json.dumps(original.model_dump()).encode(),
        )
        parsed = doc.parse(UserModel)
        assert parsed == original
        assert isinstance(parsed, UserModel)

    def test_parsed_pydantic_model_yaml(self):
        """Test parsing YAML to Pydantic model."""
        from io import BytesIO

        from pydantic import BaseModel
        from ruamel.yaml import YAML

        class ConfigModel(BaseModel):
            host: str
            port: int
            ssl: bool

        original = ConfigModel(host="localhost", port=8080, ssl=True)
        yaml = YAML()
        stream = BytesIO()
        yaml.dump(original.model_dump(), stream)

        doc = ConcreteTestDocument(name="config.yaml", content=stream.getvalue())
        parsed = doc.parse(ConfigModel)
        assert parsed == original
        assert isinstance(parsed, ConfigModel)

    def test_parsed_unsupported_type(self):
        """Test that unsupported types raise ValueError."""
        doc = ConcreteTestDocument.create_root(name="test.txt", content="text", reason="test input")

        with pytest.raises(ValueError, match="Unsupported parse type"):
            doc.parse(int)

        with pytest.raises(ValueError, match="Unsupported parse type"):
            doc.parse(float)

    def test_parsed_wrong_extension_for_type(self):
        """Test that wrong extension for requested type raises error."""
        doc = ConcreteTestDocument.create_root(name="test.txt", content="not json", reason="test input")

        # .txt file cannot be parsed as dict without being JSON/YAML
        with pytest.raises(ValueError):
            doc.parse(dict)

    def test_parsed_binary_as_str(self):
        """Test that binary content can be decoded to str."""
        doc = ConcreteTestDocument(name="test.bin", content=b"Hello UTF-8")
        assert doc.parse(str) == "Hello UTF-8"

    def test_parsed_edge_cases(self):
        """Test edge cases for parsed method."""
        # Empty string
        doc1 = ConcreteTestDocument.create_root(name="empty.txt", content="", reason="test input")
        assert doc1.parse(str) == ""
        assert doc1.parse(bytes) == b""

        # Empty JSON array
        import json

        doc2 = ConcreteTestDocument(name="empty.json", content=json.dumps([]).encode())
        assert doc2.parse(list) == []

        # Empty JSON object
        doc3 = ConcreteTestDocument(name="empty.json", content=json.dumps({}).encode())
        assert doc3.parse(dict) == {}

        # Single item JSON list
        doc4 = ConcreteTestDocument.create_root(name="single.json", content=["Only item"], reason="test input")
        assert doc4.parse(list) == ["Only item"]


class TestParseStrictDispatch:
    """Tests for strict extension-based parse dispatch (no guessing)."""

    def test_parse_md_as_dict_raises(self):
        """Markdown files cannot be parsed as structured data."""
        doc = ConcreteTestDocument.create_root(name="report.md", content="# Hello", reason="test input")
        with pytest.raises(ValueError, match=r"use .json or .yaml extension"):
            doc.parse(dict)

    def test_parse_md_as_list_raises(self):
        """Markdown files cannot be parsed as list."""
        doc = ConcreteTestDocument.create_root(name="report.md", content="# Hello", reason="test input")
        with pytest.raises(ValueError, match=r"use .json or .yaml extension"):
            doc.parse(list)

    def test_parse_md_as_str_works(self):
        """Markdown files can still be parsed as string."""
        doc = ConcreteTestDocument.create_root(name="report.md", content="# Hello", reason="test input")
        assert doc.parse(str) == "# Hello"

    def test_parse_txt_as_dict_raises(self):
        """Text files cannot be parsed as structured data."""
        doc = ConcreteTestDocument.create_root(name="data.txt", content="not json", reason="test input")
        with pytest.raises(ValueError, match=r"use .json or .yaml extension"):
            doc.parse(dict)

    def test_create_md_with_dict_raises(self):
        """Cannot create .md document with dict content."""
        with pytest.raises(ValueError, match=r"requires .json or .yaml extension"):
            ConcreteTestDocument.create_root(name="report.md", content={"key": "value"}, reason="test input")

    def test_create_md_with_list_raises(self):
        """Cannot create .md document with list content."""
        with pytest.raises(ValueError, match=r"requires .json or .yaml extension"):
            ConcreteTestDocument.create_root(name="report.md", content=["a", "b"], reason="test input")

    def test_create_parse_symmetry_json(self):
        """Create and parse are symmetric for JSON."""
        data = {"key": "value", "nested": [1, 2, 3]}
        doc = ConcreteTestDocument.create_root(name="data.json", content=data, reason="test input")
        assert doc.parse(dict) == data

    def test_create_parse_symmetry_yaml(self):
        """Create and parse are symmetric for YAML."""
        data = {"key": "value", "items": ["a", "b"]}
        doc = ConcreteTestDocument.create_root(name="config.yaml", content=data, reason="test input")
        assert doc.parse(dict) == data


class TestTriggeredByValidator:
    """Tests for the triggered_by field validator enforcing SHA256 hashes."""

    def test_valid_triggered_by_accepted(self):
        """Valid document SHA256 hashes are accepted as triggered_by."""
        source = ConcreteTestDocument.create_root(name="source.txt", content="data", reason="test input")
        doc = ConcreteTestDocument(name="derived.txt", content=b"result", triggered_by=(source.sha256,))
        assert doc.triggered_by == (source.sha256,)

    def test_invalid_triggered_by_rejected(self):
        """Non-SHA256 strings are rejected as triggered_by."""
        with pytest.raises(ValueError, match="triggered_by entry must be a document SHA256 hash"):
            ConcreteTestDocument(name="doc.txt", content=b"data", triggered_by=("not-a-hash",))

    def test_url_triggered_by_rejected(self):
        """URLs are rejected as triggered_by (triggered_by must be document hashes)."""
        with pytest.raises(ValueError, match="triggered_by entry must be a document SHA256 hash"):
            ConcreteTestDocument(name="doc.txt", content=b"data", triggered_by=("https://example.com",))

    def test_empty_triggered_by_accepted(self):
        """Empty triggered_by tuple is accepted."""
        doc = ConcreteTestDocument.create_root(name="doc.txt", content="data", reason="test input")
        assert doc.triggered_by == ()


class TestProvenanceOverlapValidator:
    """Tests for the validator rejecting same SHA256 in both derived_from and triggered_by."""

    def test_overlap_rejected(self):
        """Same SHA256 in both derived_from and triggered_by raises ValueError."""
        source = ConcreteTestDocument.create_root(name="source.txt", content="data", reason="test input")
        with pytest.raises(ValueError, match="appears in both derived_from and triggered_by"):
            ConcreteTestDocument(
                name="doc.txt",
                content=b"result",
                derived_from=(source.sha256,),
                triggered_by=(source.sha256,),
            )

    def test_no_overlap_accepted(self):
        """Different SHA256s in derived_from and triggered_by are accepted."""
        src = ConcreteTestDocument.create_root(name="src.txt", content="source", reason="test input")
        origin = ConcreteTestDocument.create_root(name="origin.txt", content="origin", reason="test input")
        doc = ConcreteTestDocument(
            name="doc.txt",
            content=b"result",
            derived_from=(src.sha256,),
            triggered_by=(origin.sha256,),
        )
        assert doc.derived_from == (src.sha256,)
        assert doc.triggered_by == (origin.sha256,)

    def test_url_source_with_sha256_origin_accepted(self):
        """URL in derived_from + SHA256 in triggered_by is fine (no overlap possible)."""
        origin = ConcreteTestDocument.create_root(name="plan.txt", content="plan", reason="test input")
        doc = ConcreteTestDocument(
            name="doc.txt",
            content=b"result",
            derived_from=("https://example.com",),
            triggered_by=(origin.sha256,),
        )
        assert len(doc.derived_from) == 1
        assert len(doc.triggered_by) == 1


class TestDerivedFromTuple:
    """Tests for derived_from as tuple[str, ...] instead of list[str]."""

    def test_derived_from_default_is_empty_tuple(self):
        """derived_from defaults to empty tuple, not empty list."""
        doc = ConcreteTestDocument.create_root(name="doc.txt", content="data", reason="test input")
        assert doc.derived_from == ()
        assert isinstance(doc.derived_from, tuple)

    def test_derived_from_is_tuple(self):
        """derived_from is stored as tuple."""
        doc = ConcreteTestDocument(name="doc.txt", content=b"data", derived_from=("https://example.com/ref1", "https://example.com/ref2"))
        assert isinstance(doc.derived_from, tuple)
        assert doc.derived_from == ("https://example.com/ref1", "https://example.com/ref2")

    def test_content_documents_returns_tuple(self):
        """content_documents property returns tuple, not list."""
        source = ConcreteTestDocument.create_root(name="src.txt", content="x", reason="test input")
        doc = ConcreteTestDocument(
            name="doc.txt",
            content=b"y",
            derived_from=(source.sha256, "https://example.com"),
        )
        assert isinstance(doc.content_documents, tuple)
        assert doc.content_documents == (source.sha256,)

    def test_content_references_returns_tuple(self):
        """content_references property returns tuple, not list."""
        source = ConcreteTestDocument.create_root(name="src.txt", content="x", reason="test input")
        doc = ConcreteTestDocument(
            name="doc.txt",
            content=b"y",
            derived_from=(source.sha256, "https://example.com"),
        )
        assert isinstance(doc.content_references, tuple)
        assert doc.content_references == ("https://example.com",)


class TestModelValidateRoundtrip:
    """Tests for model_validate() correctness including edge cases."""

    def test_empty_derived_from_roundtrip(self):
        """Empty derived_from survives serialize → deserialize roundtrip."""
        doc = ConcreteTestDocument.create_root(name="doc.txt", content="data", reason="test input")
        serialized = doc.serialize_model()
        assert serialized["derived_from"] == []  # serialized as list
        restored = ConcreteTestDocument.model_validate(serialized)
        assert restored.derived_from == ()  # restored as tuple
        assert isinstance(restored.derived_from, tuple)

    def test_derived_from_roundtrip(self):
        """Non-empty derived_from survives serialize → deserialize roundtrip."""
        doc = ConcreteTestDocument(name="doc.txt", content=b"data", derived_from=("https://example.com/ref1", "https://example.com/ref2"))
        serialized = doc.serialize_model()
        restored = ConcreteTestDocument.model_validate(serialized)
        assert restored.derived_from == ("https://example.com/ref1", "https://example.com/ref2")

    def test_missing_derived_from_key_in_dict(self):
        """model_validate with no 'derived_from' key defaults to empty tuple."""
        restored = ConcreteTestDocument.model_validate({
            "name": "doc.txt",
            "content": "data",
        })
        assert restored.derived_from == ()

    def test_missing_triggered_by_key_in_dict(self):
        """model_validate with no 'triggered_by' key defaults to empty tuple."""
        restored = ConcreteTestDocument.model_validate({
            "name": "doc.txt",
            "content": "data",
        })
        assert restored.triggered_by == ()


class TestMimeTypeProperty:
    """Tests for the unified mime_type property (formerly detected_mime_type)."""

    def test_document_mime_type(self):
        """Document.mime_type returns detected MIME type."""
        doc = ConcreteTestDocument.create_root(name="data.json", content={"key": "value"}, reason="test input")
        assert doc.mime_type == "application/json"

    def test_document_mime_type_text(self):
        """Text documents get text MIME type."""
        doc = ConcreteTestDocument.create_root(name="readme.md", content="# Hello", reason="test input")
        assert "text" in doc.mime_type

    def test_attachment_mime_type(self):
        """Attachment.mime_type returns detected MIME type."""
        att = Attachment(name="notes.txt", content=b"Hello")
        assert "text" in att.mime_type

    def test_attachment_no_detected_mime_type(self):
        """Attachment has no detected_mime_type attribute (renamed to mime_type)."""
        att = Attachment(name="notes.txt", content=b"Hello")
        assert not hasattr(att, "detected_mime_type")

    def test_document_no_detected_mime_type(self):
        """Document has no detected_mime_type attribute (renamed to mime_type)."""
        doc = ConcreteTestDocument.create_root(name="test.txt", content="hello", reason="test input")
        assert not hasattr(doc, "detected_mime_type")


class TestConvertContentHelpers:
    """Tests for the _convert_content module-level helper."""

    def test_bytes_passthrough(self):
        from ai_pipeline_core.documents.document import _convert_content

        assert _convert_content("test.bin", b"raw") == b"raw"

    def test_str_to_utf8(self):
        from ai_pipeline_core.documents.document import _convert_content

        assert _convert_content("test.txt", "hello") == b"hello"

    def test_dict_to_json(self):
        import json

        from ai_pipeline_core.documents.document import _convert_content

        result = _convert_content("data.json", {"key": "value"})
        assert json.loads(result) == {"key": "value"}

    def test_dict_to_yaml(self):
        from ai_pipeline_core.documents.document import _convert_content

        result = _convert_content("config.yaml", {"key": "value"})
        assert b"key: value" in result

    def test_list_to_json(self):
        import json

        from ai_pipeline_core.documents.document import _convert_content

        result = _convert_content("items.json", ["a", "b"])
        assert json.loads(result) == ["a", "b"]

    def test_dict_with_txt_raises(self):
        from ai_pipeline_core.documents.document import _convert_content

        with pytest.raises(ValueError, match=r"requires .json or .yaml extension"):
            _convert_content("data.txt", {"key": "value"})

    def test_unsupported_type_raises(self):
        from ai_pipeline_core.documents.document import _convert_content

        with pytest.raises(ValueError, match="Unsupported content type"):
            _convert_content("test.txt", 42)  # type: ignore[arg-type]


class TestDocumentSizeWithAttachments:
    """Test size property includes attachment sizes."""

    def test_size_unchanged_without_attachments(self):
        """Size is identical to content length when attachments is empty."""
        doc = ConcreteTestDocument(name="test.txt", content=b"Hello")
        assert doc.size == 5

    def test_size_includes_attachments(self):
        """Size includes both content and attachment sizes."""
        doc = ConcreteTestDocument(
            name="test.txt",
            content=b"Hello",  # 5 bytes
            attachments=(
                Attachment(name="a.txt", content=b"abc"),  # 3 bytes
                Attachment(name="b.bin", content=b"\x00\x01"),  # 2 bytes
            ),
        )
        assert doc.size == 5 + 3 + 2


class TestDocumentTotalSizeValidation:
    """Test validate_total_size model validator."""

    def test_content_only_oversized_rejected_by_field_validator(self):
        """Content exceeding MAX_CONTENT_SIZE is rejected by field_validator (early fail-fast)."""
        with pytest.raises(DocumentSizeError, match="exceeds maximum allowed size"):
            SmallDocument(name="test.txt", content=b"12345678901")  # 11 bytes > limit 10

    def test_content_plus_attachments_exceeding_limit(self):
        """Content + attachments exceeding MAX_CONTENT_SIZE is rejected by model_validator."""
        # Content is 7 bytes (under 10-byte limit), but total with attachment is 12
        with pytest.raises(DocumentSizeError, match="including attachments"):
            SmallDocument(
                name="test.txt",
                content=b"1234567",  # 7 bytes
                attachments=(Attachment(name="a.txt", content=b"12345"),),  # 5 bytes => total 12
            )

    def test_content_plus_attachments_within_limit(self):
        """Content + attachments within MAX_CONTENT_SIZE passes."""
        doc = SmallDocument(
            name="test.txt",
            content=b"1234",  # 4 bytes
            attachments=(Attachment(name="a.txt", content=b"123"),),  # 3 bytes => total 7
        )
        assert doc.size == 7

    def test_name_rejects_meta_json_extension(self):
        """Document names ending with .meta.json are rejected (reserved for store)."""
        with pytest.raises(DocumentNameError, match=r"\.meta\.json"):
            ConcreteTestDocument(name="data.meta.json", content=b"test")
