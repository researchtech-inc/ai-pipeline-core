"""Tests for Document.create classmethod with various content types."""

from io import BytesIO
from typing import Any

import pytest
from pydantic import BaseModel
from ruamel.yaml import YAML

from ai_pipeline_core.documents import Document
from ai_pipeline_core.exceptions import DocumentNameError, DocumentSizeError


class ConcreteTestDocument(Document):
    """Concrete document class for testing."""

    def get_type(self) -> str:
        return "test"


class SmallDocument(Document):
    """Document with small size limit for testing."""

    MAX_CONTENT_SIZE = 10

    def get_type(self) -> str:
        return "small"


class TestCreateMethod:
    """Tests for the Document.create classmethod."""

    def test_create_with_str_content(self):
        """Test create with string content."""
        text = "Hello ✨"
        doc = ConcreteTestDocument.create_root(
            name="greeting.txt",
            description="str content",
            content=text,
            reason="test input",
        )
        assert doc.content == text.encode("utf-8")
        assert doc.text == text
        assert doc.is_text

    def test_create_with_bytes_content(self):
        """Test create with bytes content."""
        raw = b"\x00\xffdata"
        doc = ConcreteTestDocument.create_root(
            name="raw.bin",
            description=None,
            content=raw,
            reason="test input",
        )
        assert doc.content == raw
        assert doc.size == len(raw)
        assert not doc.is_text

    def test_create_validation_applies(self):
        """Test that validation still applies through create()."""
        # Name validation still applies through create()
        with pytest.raises(DocumentNameError):
            ConcreteTestDocument.create_root(name="../hack.txt", description=None, content="x", reason="test input")

        # Size validation applies too (using SmallDocument)
        doc_ok = SmallDocument.create_root(name="ok.txt", description=None, content="12345", reason="test input")
        assert doc_ok.size == 5
        with pytest.raises(DocumentSizeError):
            SmallDocument.create_root(name="big.txt", description=None, content="12345678901", reason="test input")

    def test_create_with_dict_json(self):
        """Test create with dictionary for JSON file."""
        data = {"key": "value", "number": 42, "list": [1, 2, 3]}
        doc = ConcreteTestDocument.create_root(name="test.json", description="JSON from dict", content=data, reason="test input")

        assert doc.is_text
        assert doc.mime_type == "application/json"

        # Should be valid JSON
        parsed = doc.as_json()
        assert parsed == data

    def test_create_with_dict_yaml(self):
        """Test create with dictionary for YAML file."""
        data = {"database": {"host": "localhost", "port": 5432}, "cache": {"ttl": 300}}
        doc = ConcreteTestDocument.create_root(name="config.yaml", description="YAML config", content=data, reason="test input")

        assert doc.is_text
        assert doc.mime_type == "application/yaml"

        # Should be valid YAML
        yaml = YAML()
        parsed = yaml.load(BytesIO(doc.content))
        assert parsed == data

    def test_create_with_pydantic_model_json(self):
        """Test create with Pydantic model for JSON file."""

        class UserModel(BaseModel):
            username: str
            email: str
            active: bool = True

        user = UserModel(username="alice", email="alice@example.com")
        doc = ConcreteTestDocument.create_root(name="user.json", description="User data", content=user, reason="test input")

        assert doc.is_text
        assert doc.mime_type == "application/json"

        # Should be valid JSON with model data
        parsed = doc.as_json()
        assert parsed["username"] == "alice"
        assert parsed["email"] == "alice@example.com"
        assert parsed["active"] is True

        # Should roundtrip
        restored = UserModel(**parsed)
        assert restored == user

    def test_create_with_pydantic_model_yaml(self):
        """Test create with Pydantic model for YAML file."""

        class ServerConfig(BaseModel):
            name: str
            workers: int
            ssl_enabled: bool

        config = ServerConfig(name="api-server", workers=4, ssl_enabled=True)
        doc = ConcreteTestDocument.create_root(name="server.yaml", description="Server config", content=config, reason="test input")

        assert doc.is_text
        assert doc.mime_type == "application/yaml"

        # Should be valid YAML with model data
        yaml = YAML()
        parsed = yaml.load(BytesIO(doc.content))
        assert parsed["name"] == "api-server"
        assert parsed["workers"] == 4
        assert parsed["ssl_enabled"] is True

        # Should roundtrip
        restored = ServerConfig(**parsed)
        assert restored == config

    def test_create_with_list_json(self):
        """Test create with list for JSON file."""
        data = ["item1", "item2", "item3"]
        doc = ConcreteTestDocument.create_root(name="list.json", description="JSON list", content=data, reason="test input")

        assert doc.is_text
        assert doc.mime_type == "application/json"

        # Should be valid JSON
        parsed = doc.as_json()
        assert parsed == data

    def test_create_with_list_yaml(self):
        """Test create with list for YAML file."""
        data = ["item1", "item2", "item3"]
        doc = ConcreteTestDocument.create_root(name="list.yaml", description="YAML list", content=data, reason="test input")

        assert doc.is_text
        assert doc.mime_type == "application/yaml"

        # Should be valid YAML
        yaml = YAML()
        parsed = yaml.load(BytesIO(doc.content))
        assert parsed == data

    def test_create_with_list_json_content(self):
        """Test create with list for JSON file."""
        items = ["Section 1", "Section 2", "Section 3"]
        doc = ConcreteTestDocument.create_root(name="sections.json", content=items, reason="test input")
        assert doc.is_text
        assert doc.parse(list) == items

    def test_create_with_list_unsupported_extension_raises(self):
        """Test that list content with .md extension raises error (not JSON/YAML)."""
        with pytest.raises(ValueError, match=r"requires .json or .yaml extension"):
            ConcreteTestDocument.create_root(name="sections.md", content=["a", "b"], reason="test input")

    def test_create_with_list_of_models_json(self):
        """Test create with list of Pydantic models for JSON file."""

        class SampleModel(BaseModel):
            name: str
            value: int

        models = [
            SampleModel(name="first", value=1),
            SampleModel(name="second", value=2),
        ]

        doc = ConcreteTestDocument.create_root(name="models.json", description="List of models", content=models, reason="test input")

        assert doc.is_text
        assert doc.mime_type == "application/json"

        # Should be valid JSON with list of model data
        parsed = doc.as_json()
        assert len(parsed) == 2
        assert parsed[0]["name"] == "first"
        assert parsed[1]["value"] == 2

        # Should roundtrip
        restored = [SampleModel(**item) for item in parsed]
        assert restored == models

    def test_create_with_list_of_models_yaml(self):
        """Test create with list of Pydantic models for YAML file."""

        class SampleModel(BaseModel):
            name: str
            value: int

        models = [
            SampleModel(name="first", value=1),
            SampleModel(name="second", value=2),
        ]

        doc = ConcreteTestDocument.create_root(name="models.yaml", description="List of models", content=models, reason="test input")

        assert doc.is_text
        assert doc.mime_type == "application/yaml"

        # Should be valid YAML with list of model data
        yaml = YAML()
        parsed = yaml.load(BytesIO(doc.content))
        assert len(parsed) == 2
        assert parsed[0]["name"] == "first"
        assert parsed[1]["value"] == 2

        # Should roundtrip
        restored = [SampleModel(**item) for item in parsed]
        assert restored == models

    def test_create_concrete_document(self):
        """Test creating concrete Document subclass with various content types."""
        doc1 = ConcreteTestDocument.create_root(name="temp.txt", content="Temporary content", description="Test temp doc", reason="test input")
        assert doc1.text == "Temporary content"

        doc2 = ConcreteTestDocument.create_root(name="temp.json", content={"temp": "data"}, reason="test input")
        assert doc2.as_json() == {"temp": "data"}

        class TempModel(BaseModel):
            value: str

        model = TempModel(value="test")
        doc3 = ConcreteTestDocument.create_root(name="temp.json", content=model, reason="test input")
        assert doc3.as_json()["value"] == "test"

    # Removed test_create_with_numeric_content and test_create_with_boolean_content
    # as int/float/bool are not supported content types for create method

    def test_create_with_yml_extension(self):
        """Test create accepts .yml extension for YAML."""
        data = {"test": "value"}
        doc = ConcreteTestDocument.create_root(name="config.yml", content=data, reason="test input")
        assert doc.mime_type == "application/yaml"

    def test_json_yaml_roundtrip(self):
        """Test that JSON/YAML creation and parsing roundtrips correctly."""

        class DataModel(BaseModel):
            items: list[str]
            metadata: dict[str, Any]

        original = DataModel(items=["a", "b", "c"], metadata={"count": 3, "version": 1})

        # JSON roundtrip
        json_doc = ConcreteTestDocument.create_root(name="data.json", content=original, reason="test input")
        json_model = DataModel(**json_doc.as_json())
        assert json_model == original

        # YAML roundtrip
        yaml_doc = ConcreteTestDocument.create_root(name="data.yaml", content=original, reason="test input")
        yaml = YAML()
        yaml_data = yaml.load(BytesIO(yaml_doc.content))
        yaml_model = DataModel(**yaml_data)
        assert yaml_model == original

    def test_create_with_unsupported_content_type(self):
        """Test that unsupported content types raise an error."""

        # Custom class
        class CustomClass:
            pass

        obj = CustomClass()
        with pytest.raises(ValueError) as exc_info:
            ConcreteTestDocument.create_root(
                name="test.txt",
                content=obj,  # type: ignore[arg-type]
                reason="test input",
            )
        assert "Unsupported content type" in str(exc_info.value)

    def test_create_with_dict_non_json_yaml(self):
        """Test that dict content for non-JSON/YAML files raises error."""
        with pytest.raises(ValueError, match=r"requires .json or .yaml extension"):
            ConcreteTestDocument.create_root(name="test.txt", content={"key": "value"}, reason="test input")

    def test_create_with_list_non_json_yaml(self):
        """Test that list content for non-JSON/YAML files raises error."""
        with pytest.raises(ValueError, match=r"requires .json or .yaml extension"):
            ConcreteTestDocument.create_root(name="test.txt", content=["item1", "item2"], reason="test input")

    def test_create_with_list_md_raises(self):
        """Test that list content with .md extension raises error."""
        with pytest.raises(ValueError, match=r"requires .json or .yaml extension"):
            ConcreteTestDocument.create_root(name="test.md", content=["a", "b"], reason="test input")
