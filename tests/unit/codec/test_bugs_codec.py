"""Bug-proving tests for codec boundary issues.

- model_validate() error must be wrapped as CodecError, not escape as raw ValidationError
- Function-local classes (<locals> in qualname) must be rejected at encode time
- Citation inside ModelResponse must survive codec encoding (was a dataclass, now BaseModel)
"""

from typing import Any

import pytest
from pydantic import BaseModel, Field

from ai_pipeline_core._codec import CodecError, UniversalCodec
from ai_pipeline_core._llm_core.model_response import Citation, ModelResponse
from ai_pipeline_core._llm_core.types import TokenUsage


class _StrictModel(BaseModel):
    count: int = Field(ge=0)


class _FailingLoadModel(BaseModel):
    value: str

    @classmethod
    def codec_load(cls, data: dict[str, Any]) -> _FailingLoadModel:
        raise RuntimeError("Load failed intentionally")

    def codec_state(self) -> dict[str, Any]:
        return {"value": self.value}


async def test_codec_decode_async_wraps_validation_error_as_codec_error() -> None:
    """When a Pydantic model's data fails validation during decode_async, the error
    must be wrapped as CodecError, not escape as raw ValidationError.

    Callers expect CodecError from the decode path (error boundary).
    """
    codec = UniversalCodec()
    encoded = codec.encode(_StrictModel(count=5))
    payload = encoded.value
    # Corrupt: set count to invalid value that fails Pydantic validation
    payload["data"]["count"] = "not_an_integer"

    with pytest.raises(CodecError):
        await codec.decode_async(payload, db=None)


async def test_codec_decode_async_wraps_codec_load_error_as_codec_error() -> None:
    """When codec_load raises, the error must be wrapped as CodecError."""
    codec = UniversalCodec()
    encoded = codec.encode(_FailingLoadModel(value="test"))
    payload = encoded.value

    with pytest.raises(CodecError):
        await codec.decode_async(payload, db=None)


def test_codec_encode_rejects_function_local_class() -> None:
    """A class defined inside a function has '<locals>' in __qualname__,
    making it unresolvable during replay. The codec should reject it at encode time.
    """

    def create_local_model() -> type[BaseModel]:
        class LocalModel(BaseModel):
            x: int = 1

        return LocalModel

    LocalModel = create_local_model()
    assert "<locals>" in LocalModel.__qualname__

    codec = UniversalCodec()
    with pytest.raises(CodecError, match="<locals>"):
        codec.encode(LocalModel(x=42))


def test_codec_encodes_model_response_with_citations() -> None:
    """ModelResponse with non-empty citations must survive codec encoding.

    Regression: Citation was a frozen dataclass which the codec couldn't encode.
    Fixed by converting Citation to a frozen BaseModel and adding dataclass
    support to the codec.
    """
    citation = Citation(title="Grammy Awards 2026", url="https://example.com/grammys", start_index=0, end_index=10)
    response = ModelResponse[str](
        content="Bad Bunny won Album of the Year",
        parsed="Bad Bunny won Album of the Year",
        model="gemini-3-flash",
        usage=TokenUsage(prompt_tokens=10, completion_tokens=20, total_tokens=30),
        citations=(citation,),
    )
    codec = UniversalCodec()
    codec.encode(response)
