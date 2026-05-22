"""Tests for the universal replay codec."""

import json
from collections.abc import Generator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from pydantic import BaseModel, ConfigDict, Field

from ai_pipeline_core._codec import CodecImportError
from ai_pipeline_core._codec import CodecCycleError, CodecError, EncodeResult, EnumDecodeError, SerializedError, UniversalCodec
from ai_pipeline_core._llm_core.model_response import ModelResponse
from ai_pipeline_core._llm_core.types import TokenUsage
from ai_pipeline_core.database import HydratedDocument
from ai_pipeline_core.database._types import _BlobRecord
from ai_pipeline_core.documents import Attachment, Document
from ai_pipeline_core.documents._hashing import compute_content_sha256
from ai_pipeline_core.llm.conversation import AssistantMessage, Conversation, UserMessage
from ai_pipeline_core.llm.tools import Tool, ToolCallRecord, ToolOutput
from tests.support.model_catalog import DEFAULT_TEST_MODEL


@pytest.fixture(autouse=True)
def suppress_document_registration() -> Generator[None]:
    return


class CodecDocument(Document):
    """Document used by codec tests."""


class CodecMode(StrEnum):
    """Enum used by codec tests."""

    FAST = "fast"
    DEEP = "deep"


class PayloadModel(BaseModel):
    """BaseModel used by codec tests."""

    model_config = ConfigDict(frozen=True)

    name: str
    count: int
    created_at: datetime
    request_id: UUID


class WeatherTool(Tool):
    """Test tool used by Conversation codec round-trips."""

    class Input(BaseModel):
        city: str = Field(description="City name")

    class Output(BaseModel):
        weather: str

    async def run(self, input: Input) -> Output:
        return self.Output(weather=f"Weather for {input.city}")


class NestedPayloadModel(BaseModel):
    """BaseModel used to verify recursive codec handling inside ordinary models."""

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    document: CodecDocument
    blob: bytes
    values: tuple[int, int]
    tool_type: type[WeatherTool]


@dataclass(slots=True)
class FakeDatabase:
    """Minimal async database surface used by codec decode tests."""

    blobs: dict[str, _BlobRecord] = field(default_factory=dict)
    documents: dict[str, HydratedDocument] = field(default_factory=dict)

    async def get_blob(self, content_sha256: str) -> _BlobRecord | None:
        return self.blobs.get(content_sha256)

    async def get_document_with_content(self, document_sha256: str) -> HydratedDocument | None:
        return self.documents.get(document_sha256)


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _assert_round_trip(value: Any, *, db: FakeDatabase | None = None) -> tuple[Any, EncodeResult]:
    codec = UniversalCodec()
    encoded = codec.encode(value)
    decoded = codec.decode(encoded.value, db=db)
    re_encoded = codec.encode(decoded)
    assert _canonical_json(re_encoded.value) == _canonical_json(encoded.value)
    return decoded, encoded


def _store_document(database: FakeDatabase, document: Document) -> None:
    from ai_pipeline_core.database._documents import document_to_blobs, document_to_record

    for blob in document_to_blobs(document):
        database.blobs[blob.content_sha256] = blob
    record = document_to_record(document)
    attachment_contents = {compute_content_sha256(att.content): att.content for att in document.attachments}
    database.documents[document.sha256] = HydratedDocument(
        record=record,
        content=document.content,
        attachment_contents=attachment_contents,
    )


def test_primitives_and_containers_round_trip() -> None:
    value = {
        "none": None,
        "bool": True,
        "int": 7,
        "float": 1.5,
        "str": "hello",
        "list": [1, "two", None],
        "dict": {"nested": "value"},
    }

    decoded, encoded = _assert_round_trip(value)

    assert decoded == value
    assert encoded.value["list"] == [1, "two", None]


def test_empty_containers_round_trip() -> None:
    value = {
        "list": [],
        "tuple": (),
        "dict": {},
    }

    decoded, encoded = _assert_round_trip(value)

    assert decoded == value
    assert encoded.value == {
        "list": [],
        "tuple": {"$type": "tuple", "items": []},
        "dict": {},
    }


def test_tuple_preserves_distinction_from_list() -> None:
    value = ([1, 2], (1, 2))

    decoded, encoded = _assert_round_trip(value)

    assert decoded == value
    assert isinstance(decoded[0], list)
    assert isinstance(decoded[1], tuple)
    assert encoded.value == {
        "$type": "tuple",
        "items": [
            [1, 2],
            {"$type": "tuple", "items": [1, 2]},
        ],
    }


def test_uuid_envelope_round_trip() -> None:
    value = uuid4()

    decoded, encoded = _assert_round_trip(value)

    assert decoded == value
    assert encoded.value == {"$type": "uuid", "value": str(value)}


def test_datetime_envelope_round_trip() -> None:
    value = datetime(2026, 3, 12, 10, 0, tzinfo=UTC)

    decoded, encoded = _assert_round_trip(value)

    assert decoded == value
    assert encoded.value == {"$type": "datetime", "value": "2026-03-12T10:00:00+00:00"}


def test_path_envelope_round_trip() -> None:
    value = Path("/tmp/workdir")

    decoded, encoded = _assert_round_trip(value)

    assert decoded == value
    assert encoded.value == {"$type": "path", "value": "/tmp/workdir"}


def test_datetime_requires_utc_offset() -> None:
    codec = UniversalCodec()

    with pytest.raises(CodecError, match="aware datetime with an explicit UTC offset"):
        codec.encode(datetime(2026, 3, 12, 10, 0))


def test_enum_round_trip_uses_name_first_then_value() -> None:
    codec = UniversalCodec()
    encoded = codec.encode(CodecMode.FAST)

    assert encoded.value == {
        "$type": "enum",
        "class_path": "tests.unit.codec.test_codec:CodecMode",
        "name": "FAST",
        "value": "fast",
    }
    assert codec.decode(encoded.value) is CodecMode.FAST

    fallback_payload = dict(encoded.value)
    fallback_payload["name"] = "MISSING"
    assert codec.decode(fallback_payload) is CodecMode.FAST

    invalid_payload = dict(fallback_payload)
    invalid_payload["value"] = "missing"
    with pytest.raises(EnumDecodeError, match="no member named"):
        codec.decode(invalid_payload)


def test_type_ref_round_trip() -> None:
    decoded, encoded = _assert_round_trip(PayloadModel)

    assert decoded is PayloadModel
    assert encoded.value == {"$type": "type_ref", "path": "tests.unit.codec.test_codec:PayloadModel"}


def test_pydantic_envelope_round_trip() -> None:
    value = PayloadModel(
        name="payload",
        count=3,
        created_at=datetime(2026, 3, 12, 10, 0, tzinfo=UTC),
        request_id=UUID("12345678-1234-5678-1234-567812345678"),
    )

    decoded, encoded = _assert_round_trip(value)

    assert decoded == value
    assert encoded.value["$type"] == "pydantic"
    assert encoded.value["class_path"] == "tests.unit.codec.test_codec:PayloadModel"


def test_pydantic_round_trip_recursively_encodes_nested_special_values() -> None:
    document = CodecDocument(
        name="nested.txt",
        content=b"nested",
        derived_from=("https://example.com/nested",),
    )
    database = FakeDatabase()
    _store_document(database, document)
    payload_sha = compute_content_sha256(b"payload")
    database.blobs[payload_sha] = _BlobRecord(content_sha256=payload_sha, content=b"payload")
    value = NestedPayloadModel(
        document=document,
        blob=b"payload",
        values=(1, 2),
        tool_type=WeatherTool,
    )

    decoded, encoded = _assert_round_trip(value, db=database)

    assert decoded == value
    assert encoded.document_shas == frozenset({document.sha256})
    assert encoded.blob_shas == frozenset({compute_content_sha256(b"payload")})
    assert encoded.value["data"]["document"]["$type"] == "document_ref"
    assert encoded.value["data"]["blob"]["$type"] == "blob_ref"
    assert encoded.value["data"]["values"] == {"$type": "tuple", "items": [1, 2]}
    assert encoded.value["data"]["tool_type"] == {"$type": "type_ref", "path": "tests.unit.codec.test_codec:WeatherTool"}


@dataclass(frozen=True, slots=True)
class CodecPoint:
    """Frozen dataclass used by codec tests."""

    x: int
    y: int
    label: str


def test_dataclass_round_trip() -> None:
    value = CodecPoint(x=1, y=2, label="origin")

    decoded, encoded = _assert_round_trip(value)

    assert decoded == value
    assert encoded.value["$type"] == "dataclass"
    assert encoded.value["class_path"] == "tests.unit.codec.test_codec:CodecPoint"
    assert encoded.value["data"] == {"x": 1, "y": 2, "label": "origin"}


def test_dataclass_inside_container_round_trip() -> None:
    value = [CodecPoint(x=1, y=2, label="a"), CodecPoint(x=3, y=4, label="b")]

    decoded, encoded = _assert_round_trip(value)

    assert decoded == value
    assert all(isinstance(v, CodecPoint) for v in decoded)


def test_frozenset_round_trip() -> None:
    value = frozenset({"b", "a"})

    decoded, encoded = _assert_round_trip(value)

    assert decoded == value
    assert encoded.value == {"$type": "frozenset", "items": ["a", "b"]}


def test_model_response_with_citations_round_trip() -> None:
    from ai_pipeline_core._llm_core.model_response import Citation

    citation = Citation(title="Test", url="https://example.com", start_index=0, end_index=5)
    response = ModelResponse[Any](
        content="Hello",
        parsed="Hello",
        model="test-model",
        usage=TokenUsage(prompt_tokens=1, completion_tokens=2, total_tokens=3),
        citations=(citation,),
    )

    decoded, encoded = _assert_round_trip(response)

    assert isinstance(decoded, ModelResponse)
    assert len(decoded.citations) == 1
    assert decoded.citations[0].title == "Test"
    assert decoded.citations[0].url == "https://example.com"


def test_serialized_error_uses_pydantic_envelope() -> None:
    value = SerializedError(
        error_class_path="builtins:ValueError",
        type_name="ValueError",
        message="boom",
        traceback_text="Traceback...",
    )

    decoded, encoded = _assert_round_trip(value)

    assert decoded == value
    assert encoded.value["$type"] == "pydantic"


def test_bytes_encode_to_blob_ref_and_decode_with_memoized_sha() -> None:
    value = [b"abc", b"abc"]
    database = FakeDatabase()
    blob_sha = compute_content_sha256(b"abc")
    database.blobs[blob_sha] = _BlobRecord(content_sha256=blob_sha, content=b"abc")

    decoded, encoded = _assert_round_trip(value, db=database)

    assert decoded == value
    assert decoded[0] is decoded[1]
    assert encoded.blob_shas == frozenset({blob_sha})
    assert encoded.value == [
        {"$type": "blob_ref", "sha256": blob_sha},
        {"$type": "blob_ref", "sha256": blob_sha},
    ]


def test_document_encode_to_document_ref_and_decode_with_attachments() -> None:
    document = CodecDocument(
        name="report.md",
        content=b"# Report",
        description="Codec document",
        derived_from=("https://example.com/report",),
        attachments=(Attachment(name="details.txt", content="details", description="Attachment text"),),
    )
    database = FakeDatabase()
    _store_document(database, document)

    decoded, encoded = _assert_round_trip((document, document), db=database)

    assert decoded[0] == document
    assert decoded[0] is decoded[1]
    assert decoded[0].attachments == document.attachments
    assert encoded.document_shas == frozenset({document.sha256})
    assert encoded.value == {
        "$type": "tuple",
        "items": [
            {
                "$type": "document_ref",
                "sha256": document.sha256,
                "class_path": "tests.unit.codec.test_codec:CodecDocument",
            },
            {
                "$type": "document_ref",
                "sha256": document.sha256,
                "class_path": "tests.unit.codec.test_codec:CodecDocument",
            },
        ],
    }


def test_conversation_round_trip_preserves_private_fields() -> None:
    record = ToolCallRecord(
        tool=WeatherTool,
        input=WeatherTool.Input(city="Paris"),
        output=ToolOutput(content="Sunny"),
        round=2,
    )
    conversation = Conversation(
        model=DEFAULT_TEST_MODEL,
        messages=(UserMessage("Hello"), AssistantMessage("Hi")),
        include_date=False,
        current_date=None,
    ).model_copy(
        update={
            "_conversation_id": "conversation-123",
            "_tool_call_records": (record,),
        }
    )

    decoded, encoded = _assert_round_trip(conversation)

    assert isinstance(decoded, Conversation)
    assert decoded.model == DEFAULT_TEST_MODEL
    assert decoded.messages == conversation.messages
    assert decoded._conversation_id == "conversation-123"
    assert decoded.tool_call_records == (record,)
    assert encoded.value["$type"] == "pydantic"
    assert encoded.value["data"]["_conversation_id"] == "conversation-123"
    assert encoded.value["data"]["_tool_call_records"] == {
        "$type": "tuple",
        "items": [
            {
                "tool": {"$type": "type_ref", "path": "tests.unit.codec.test_codec:WeatherTool"},
                "input": {
                    "$type": "pydantic",
                    "class_path": "tests.unit.codec.test_codec:WeatherTool.Input",
                    "data": {"city": "Paris"},
                },
                "output": {
                    "$type": "pydantic",
                    "class_path": "ai_pipeline_core.llm.tools:ToolOutput",
                    "data": {"content": "Sunny", "data": None},
                },
                "round": 2,
            }
        ],
    }


def test_conversation_round_trip_with_model_response_history() -> None:
    response = ModelResponse[Any](
        content="Done",
        parsed="Done",
        model="test-model",
        usage=TokenUsage(prompt_tokens=1, completion_tokens=2, total_tokens=3),
    )
    conversation = Conversation(
        model=DEFAULT_TEST_MODEL,
        messages=(UserMessage("Hello"), response),
        include_date=False,
        current_date=None,
    )

    decoded, encoded = _assert_round_trip(conversation)

    assert isinstance(decoded.messages[1], ModelResponse)
    assert decoded.messages[1].content == "Done"
    encoded_messages = encoded.value["data"]["messages"]
    assert encoded_messages["$type"] == "tuple"
    assert encoded_messages["items"][0] == {"__conversation_message__": "user", "text": "Hello"}
    response_payload = encoded_messages["items"][1]
    assert response_payload["$type"] == "pydantic"
    assert response_payload["class_path"] == "ai_pipeline_core._llm_core.model_response:ModelResponse"
    assert response_payload["data"]["content"] == "Done"
    assert response_payload["data"]["model"] == "test-model"
    assert response_payload["data"]["usage"]["data"]["total_tokens"] == 3


def test_cycle_detection_raises_codec_cycle_error() -> None:
    value: list[Any] = []
    value.append(value)

    with pytest.raises(CodecCycleError, match="reference cycle"):
        UniversalCodec().encode(value)


def test_unsupported_types_raise_codec_error() -> None:
    with pytest.raises(CodecError, match="cannot encode value"):
        UniversalCodec().encode({1, 2, 3})


def test_non_string_dict_keys_raise_codec_error() -> None:
    with pytest.raises(CodecError, match="expected str"):
        UniversalCodec().encode({1: "value"})


def test_type_key_collision_is_escaped_and_unescaped() -> None:
    value = {
        "$type": "user-owned",
        "nested": {
            "$type": "child",
        },
    }

    decoded, encoded = _assert_round_trip(value)

    assert decoded == value
    assert encoded.value == {
        "$$type": "user-owned",
        "nested": {
            "$$type": "child",
        },
    }


def test_type_key_collision_preserves_literal_double_dollar_type_keys() -> None:
    value = {
        "$type": "single",
        "$$type": "double",
        "nested": {
            "$$type": "child-double",
        },
    }

    decoded, encoded = _assert_round_trip(value)

    assert decoded == value
    assert encoded.value == {
        "$$type": "single",
        "$$$type": "double",
        "nested": {
            "$$$type": "child-double",
        },
    }


def test_decode_document_ref_requires_database() -> None:
    with pytest.raises(CodecError, match="without a DatabaseReader"):
        UniversalCodec().decode({
            "$type": "document_ref",
            "sha256": "doc-sha",
            "class_path": "tests.unit.codec.test_codec:CodecDocument",
        })


def test_decode_blob_ref_requires_database() -> None:
    with pytest.raises(CodecError, match="without a DatabaseReader"):
        UniversalCodec().decode({
            "$type": "blob_ref",
            "sha256": "blob-sha",
        })


def test_decode_type_ref_invalid_import_path_raises_codec_import_error() -> None:
    with pytest.raises(CodecImportError, match=r"--import missing\.module"):
        UniversalCodec().decode({"$type": "type_ref", "path": "missing.module:MissingType"})


@pytest.mark.asyncio
async def test_decode_async_resolves_database_refs_without_blocking() -> None:
    document = CodecDocument(
        name="async.txt",
        content=b"async",
        derived_from=("https://example.com/async",),
    )
    database = FakeDatabase()
    _store_document(database, document)
    payload = UniversalCodec().encode([document, b"payload"])
    blob_sha = compute_content_sha256(b"payload")
    database.blobs[blob_sha] = _BlobRecord(content_sha256=blob_sha, content=b"payload")

    decoded = await UniversalCodec().decode_async(payload.value, db=database)

    assert decoded == [document, b"payload"]


def test_sha_sets_include_documents_and_blobs() -> None:
    document = CodecDocument(
        name="bundle.txt",
        content=b"bundle",
        derived_from=("https://example.com",),
    )

    encoded = UniversalCodec().encode({"document": document, "blob": b"payload"})

    assert encoded.document_shas == frozenset({document.sha256})
    assert encoded.blob_shas == frozenset({compute_content_sha256(b"payload")})


# ── Phase 7f: ToolOutput.data codec round-trip ───────────────────────────────


def test_tool_output_round_trip_with_data_field() -> None:
    """ToolOutput with data=SomeBaseModel round-trips through codec correctly."""
    weather = WeatherTool.Output(weather="Sunny")
    output = ToolOutput(content='{"weather": "Sunny"}', data=weather)
    decoded, encoded = _assert_round_trip(output)
    assert decoded.content == '{"weather": "Sunny"}'
    assert decoded.data is not None


def test_tool_output_round_trip_data_none() -> None:
    """ToolOutput with data=None round-trips correctly."""
    output = ToolOutput(content="error message")
    decoded, encoded = _assert_round_trip(output)
    assert decoded.content == "error message"
    assert decoded.data is None


def test_old_tool_output_without_data_field_decodes() -> None:
    """Encoded ToolOutput from before redesign (no data key) decodes with data=None."""
    codec = UniversalCodec()
    old_encoded = {
        "$type": "pydantic",
        "class_path": "ai_pipeline_core.llm.tools:ToolOutput",
        "data": {"content": "old format"},
    }
    decoded = codec.decode(old_encoded)
    assert isinstance(decoded, ToolOutput)
    assert decoded.content == "old format"
    assert decoded.data is None
