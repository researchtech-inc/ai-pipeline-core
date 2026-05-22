"""Capability probes for arbitrary AIModel inputs.

Each probe runs through ``_llm_core.client.generate`` (not Conversation) so
the signal is model-level. For flag-gated axes we construct a probe-copy
``AIModel(..., supports_X=True)`` to bypass the preflight gate; the original
declaration is preserved for reporting.
"""

import base64
import time
from collections.abc import Callable
from typing import Any

import httpx
import openai
import pytest
from pydantic import BaseModel, Field

from ai_pipeline_core._llm_core.client import generate
from ai_pipeline_core._llm_core.exceptions import LLMError, TerminalError
from ai_pipeline_core._llm_core.model_response import ModelResponse
from ai_pipeline_core._llm_core.request import LLMRequest, ResponseSpec, RetrySpec, ToolSpec
from ai_pipeline_core._llm_core.types import (
    AIModel,
    CoreMessage,
    ImageContent,
    PDFContent,
    Role,
    TextContent,
)
from ai_pipeline_core.settings import settings
from tests.capabilities._invariants import (
    has_non_empty_content,
    has_tool_call,
    marker_in_content,
    parsed_matches_model,
)
from tests.capabilities._recorder import CapabilityResult, Status
from tests.support.helpers import make_text_image, pdf_with_marker

HAS_API_KEYS = bool(settings.openai_api_key and settings.openai_base_url)

pytestmark = [
    pytest.mark.skipif(not HAS_API_KEYS, reason="LLM API credentials not configured"),
]


class _ProbeCity(BaseModel):
    """Minimal structured-output probe schema."""

    name: str = Field(description="City name.")
    country: str = Field(description="Country name.")


_AXES = ("text", "structured_output", "tools", "images", "pdfs")
_FLAG_ATTR: dict[str, str] = {
    "structured_output": "supports_structured_output",
    "tools": "supports_tools",
    "images": "supports_images",
    "pdfs": "supports_pdfs",
}

_GET_CAPITAL_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "get_capital",
        "description": "Get the capital city of a country.",
        "parameters": {
            "type": "object",
            "properties": {
                "country": {"type": "string", "description": "Country name."},
            },
            "required": ["country"],
            "additionalProperties": False,
        },
        "strict": True,
    },
}

_PDF_MARKER = "CAPMARK-987"
_IMAGE_MARKER = "BANANA"

_Evaluator = Callable[[ModelResponse[Any]], tuple[bool, str]]


def _probe_copy(model: AIModel, axis: str) -> AIModel:
    flag = _FLAG_ATTR.get(axis)
    if flag is None:
        return model
    return model.model_copy(update={flag: True})


def _declared(model: AIModel, axis: str) -> bool | None:
    flag = _FLAG_ATTR.get(axis)
    if flag is None:
        return None
    return bool(getattr(model, flag))


def _build_request(model: AIModel, messages: tuple[CoreMessage, ...], **kwargs: Any) -> LLMRequest:
    return LLMRequest(model=model, messages=messages, retry=RetrySpec(retries=0), **kwargs)


def _text_request(model: AIModel) -> LLMRequest:
    return _build_request(
        model,
        (CoreMessage(role=Role.USER, content="Reply with the single word 'ok'."),),
    )


def _eval_text(response: ModelResponse[Any]) -> tuple[bool, str]:
    if has_non_empty_content(response):
        return True, ""
    return False, "empty content"


def _structured_request(model: AIModel) -> LLMRequest:
    return _build_request(
        model,
        (CoreMessage(role=Role.USER, content="Return JSON with name='Paris' and country='France'."),),
        response=ResponseSpec(format=_ProbeCity),
    )


def _eval_structured(response: ModelResponse[Any]) -> tuple[bool, str]:
    if not parsed_matches_model(response, _ProbeCity):
        return False, f"parsed type was {type(response.parsed).__name__}"
    parsed = response.parsed
    assert isinstance(parsed, _ProbeCity)
    if not parsed.name or not parsed.country:
        return False, f"empty fields: name={parsed.name!r}, country={parsed.country!r}"
    return True, ""


def _tools_request(model: AIModel) -> LLMRequest:
    return _build_request(
        model,
        (CoreMessage(role=Role.USER, content="Use the get_capital tool to look up the capital of France."),),
        tools=ToolSpec(schemas=(_GET_CAPITAL_SCHEMA,), choice="required"),
    )


def _eval_tools(response: ModelResponse[Any]) -> tuple[bool, str]:
    if has_tool_call(response, "get_capital"):
        return True, ""
    return False, f"no get_capital call; tool_calls={len(response.tool_calls)}"


def _images_request(model: AIModel) -> LLMRequest:
    image_bytes = make_text_image(_IMAGE_MARKER, fmt="JPEG")
    image = ImageContent(data=base64.b64encode(image_bytes), mime_type="image/jpeg")
    text = TextContent(text="What single uppercase word is rendered in the image? Reply with only the word.")
    return _build_request(
        model,
        (CoreMessage(role=Role.USER, content=(text, image)),),
    )


def _eval_images(response: ModelResponse[Any]) -> tuple[bool, str]:
    if marker_in_content(response, _IMAGE_MARKER):
        return True, ""
    return False, f"marker not in content; preview={response.content[:120]!r}"


def _pdfs_request(model: AIModel) -> LLMRequest:
    pdf_bytes = pdf_with_marker(_PDF_MARKER)
    pdf = PDFContent(data=base64.b64encode(pdf_bytes), filename="probe.pdf")
    text = TextContent(text="Read the attached PDF and reply with only the marker token (the marker starts with 'CAPMARK-').")
    return _build_request(
        model,
        (CoreMessage(role=Role.USER, content=(text, pdf)),),
    )


def _eval_pdfs(response: ModelResponse[Any]) -> tuple[bool, str]:
    if marker_in_content(response, _PDF_MARKER):
        return True, ""
    return False, f"marker not in content; preview={response.content[:120]!r}"


_BUILDERS: dict[str, Callable[[AIModel], LLMRequest]] = {
    "text": _text_request,
    "structured_output": _structured_request,
    "tools": _tools_request,
    "images": _images_request,
    "pdfs": _pdfs_request,
}
_EVALUATORS: dict[str, _Evaluator] = {
    "text": _eval_text,
    "structured_output": _eval_structured,
    "tools": _eval_tools,
    "images": _eval_images,
    "pdfs": _eval_pdfs,
}


async def _run_probe(request: LLMRequest, evaluator: _Evaluator) -> tuple[Status, bool, str, float, str | None, float]:
    start = time.monotonic()
    try:
        response = await generate(request)
    except TerminalError as exc:
        return "ERROR", False, f"TerminalError: {exc!s}"[:300], 0.0, None, time.monotonic() - start
    except (openai.APIError, httpx.HTTPError, TimeoutError) as exc:  # fmt: skip
        return "ERROR", False, f"{type(exc).__name__}: {exc!s}"[:300], 0.0, None, time.monotonic() - start
    except LLMError as exc:
        return "FAIL", False, f"{type(exc).__name__}: {exc!s}"[:300], 0.0, None, time.monotonic() - start
    elapsed = time.monotonic() - start
    ok, detail = evaluator(response)
    status: Status = "PASS" if ok else "FAIL"
    cost = float(response.cost or 0.0)
    deployment_id = response.transport.aipl.deployment_id
    return status, ok, detail, cost, deployment_id, elapsed


@pytest.mark.parametrize("axis", _AXES)
async def test_capability(
    axis: str,
    models_under_test: tuple[AIModel, ...],
    record_capability: Callable[[CapabilityResult], None],
) -> None:
    """Probe ``axis`` against every model in the runtime input list.

    Each model produces one ``CapabilityResult``; the test does not assert
    PASS — the matrix records the full picture even when probes fail. The
    invocation only fails if our own probe code throws, which is a bug.
    """
    builder = _BUILDERS[axis]
    evaluator = _EVALUATORS[axis]
    for original_model in models_under_test:
        probe_model = _probe_copy(original_model, axis)
        declared = _declared(original_model, axis)
        request = builder(probe_model)
        status, probed_supported, detail, cost, deployment_id, elapsed = await _run_probe(request, evaluator)
        match = declared is None or declared == probed_supported
        record_capability(
            CapabilityResult(
                model=original_model.name,
                capability=axis,
                declared=declared,
                probed_supported=probed_supported,
                match=match,
                status=status,
                detail=detail,
                cost_usd=cost,
                elapsed_s=elapsed,
                deployment_id=deployment_id,
            )
        )
