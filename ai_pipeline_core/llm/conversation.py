"""Immutable Conversation facade over the LLM execution engine."""

import logging
from collections.abc import Sequence
from dataclasses import asdict
from datetime import date
from typing import Any, Generic, Literal, Self, TypeVar, cast, overload

from pydantic import BaseModel, ConfigDict, PrivateAttr, field_validator, model_validator

from ai_pipeline_core._llm_core import ListOf, TokenUsage
from ai_pipeline_core._llm_core.model_response import Citation, ModelResponse
from ai_pipeline_core._llm_core.request import get_list_item_type, is_list_output_type
from ai_pipeline_core._llm_core.types import AIModel, ModelOptions
from ai_pipeline_core.database import SpanKind
from ai_pipeline_core.documents import Document
from ai_pipeline_core.pipeline._execution_context import get_execution_context, get_sinks
from ai_pipeline_core.pipeline._track_span import track_span
from ai_pipeline_core.prompt_compiler.render import _RESULT_CLOSE, _extract_result, _render_multi_line_messages, render_text
from ai_pipeline_core.prompt_compiler.spec import PromptSpec

from ._conversation_codec import deserialize_message, serialize_message
from ._conversation_messages import (
    AnyMessage,
    AssistantMessage,
    ConversationContent,
    ToolResultMessage,
    UserMessage,
    _core_messages_to_span_input,
    _normalize_content,
    _prompt_parts,
    _response_format_path,
    _serialize_tool_config,
)
from ._conversation_runtime import (
    approximate_tokens_count,
    assemble_api_messages,
    build_effective_options,
    cache_overrides,
    generation_overrides,
    prepare_substitutor,
    restore_response,
    retry_overrides,
    routing_overrides,
    tool_runtime,
    validate_send_scope,
)
from ._engine import InteractionRequest, ToolRuntime, execute_interaction
from .tools import Tool, ToolCallRecord, ToolOutput

__all__ = ["AssistantMessage", "Conversation", "ConversationContent", "ToolResultMessage", "UserMessage"]

logger = logging.getLogger(__name__)

_MAX_TOOL_ROUNDS_DEFAULT = 10
_LLM_ROUND_REPLAY_TARGET = "function:ai_pipeline_core.llm._engine:_replay_llm_round"

T = TypeVar("T", default=str)
U = TypeVar("U", bound=BaseModel)


def _require_ai_model(value: object, *, context: str) -> AIModel:
    if not isinstance(value, AIModel):
        raise TypeError(f"{context} must be an AIModel, got {type(value).__name__}.")
    return value


class Conversation(BaseModel, Generic[T]):
    """Immutable document-aware conversation state."""

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    model: AIModel
    context: tuple[Document, ...] = ()
    messages: tuple[AnyMessage, ...] = ()
    model_options: ModelOptions | None = None
    enable_substitutor: bool = True
    extract_result_tags: bool = False
    include_date: bool = True
    current_date: str | None = None
    _conversation_id: str = PrivateAttr(default="")
    _tool_call_records: tuple[ToolCallRecord, ...] = PrivateAttr(default=())

    @field_validator("model", mode="before")
    @classmethod
    def _coerce_model(cls, value: Any) -> AIModel:
        return _require_ai_model(value, context="Conversation.model")

    @model_validator(mode="before")
    @classmethod
    def _initialize_defaults(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        payload = dict(cast(dict[str, Any], data))
        if payload.get("include_date", True) and "current_date" not in payload:
            payload["current_date"] = date.today().isoformat()
        model = payload.get("model")
        if "enable_substitutor" not in payload and isinstance(model, AIModel) and model.preserve_input_urls:
            payload["enable_substitutor"] = False
        return payload

    @field_validator("context", "messages", mode="before")
    @classmethod
    def _coerce_to_tuple(cls, value: list[Any] | tuple[Any, ...] | None) -> tuple[Any, ...]:
        if value is None:
            return ()
        if isinstance(value, list):
            return tuple(value)
        return value

    def codec_state(self) -> dict[str, Any]:
        """Return codec state including private fields."""
        state = {field_name: getattr(self, field_name) for field_name in type(self).model_fields}
        state["messages"] = tuple(serialize_message(message) for message in self.messages)
        state["_conversation_id"] = self._conversation_id
        state["_tool_call_records"] = tuple(
            {"tool": record.tool, "input": record.input, "output": record.output, "round": record.round} for record in self._tool_call_records
        )
        return state

    @classmethod
    def codec_load(cls, state: dict[str, Any]) -> Self:
        """Reconstruct a Conversation from codec state."""
        public = {field_name: state[field_name] for field_name in cls.model_fields if field_name in state}
        public["messages"] = tuple(deserialize_message(message) for message in public.get("messages", ()))
        conversation = cls(**public)
        conversation._conversation_id = cast(str, state.get("_conversation_id", ""))
        records = tuple(
            ToolCallRecord(
                tool=cast(type[Tool], record["tool"]),
                input=cast(BaseModel, record["input"]),
                output=cast(ToolOutput, record["output"]),
                round=cast(int, record["round"]),
            )
            for record in cast(tuple[dict[str, Any], ...], state.get("_tool_call_records", ()))
        )
        conversation._tool_call_records = records
        return conversation

    @property
    def _last_response(self) -> ModelResponse[Any] | None:
        for message in reversed(self.messages):
            if isinstance(message, ModelResponse):
                return message
        return None

    @property
    def content(self) -> str:
        """Response text from the last send call."""
        response = self._last_response
        if response is None:
            return ""
        return _extract_result(response.content) if self.extract_result_tags else response.content

    @property
    def reasoning_content(self) -> str:
        """Reasoning content from the last send call."""
        response = self._last_response
        return response.reasoning_content if response is not None else ""

    @property
    def usage(self) -> TokenUsage:
        """Token usage from the last send call."""
        response = self._last_response
        return response.usage if response is not None else TokenUsage(prompt_tokens=0, completion_tokens=0, total_tokens=0)

    @property
    def cost(self) -> float | None:
        """Generation cost from the last send call."""
        response = self._last_response
        return response.cost if response is not None else None

    @property
    def parsed(self) -> T:
        """Parsed value from the last send call."""
        response = self._last_response
        if response is None:
            raise ValueError("Cannot access .parsed before calling send(), send_structured(), or send_spec().")
        return cast(T, response.parsed)

    @property
    def citations(self) -> tuple[Citation, ...]:
        """Citations from the last send call."""
        response = self._last_response
        return response.citations if response is not None else ()

    @property
    def tool_call_records(self) -> tuple[ToolCallRecord, ...]:
        """Tool call records accumulated during the last send call."""
        return self._tool_call_records

    def tool_calls_for(self, tool_cls: type[Tool]) -> tuple[ToolCallRecord, ...]:
        """Return records for one tool class."""
        return tuple(record for record in self._tool_call_records if record.tool is tool_cls)

    async def send(
        self,
        content: ConversationContent,
        *,
        tools: list[Tool] | None = None,
        tool_choice: Literal["auto", "required", "none"] | None = None,
        max_tool_rounds: int = _MAX_TOOL_ROUNDS_DEFAULT,
        purpose: str | None = None,
    ) -> Conversation[str]:
        """Send content and return a new Conversation."""
        result = await self._execute(
            content=content,
            response_format=None,
            tools=tools,
            tool_choice=tool_choice,
            max_tool_rounds=max_tool_rounds,
            purpose=purpose,
        )
        return cast(Conversation[str], result)

    async def send_structured(
        self,
        content: ConversationContent,
        response_format: Any,
        *,
        tools: list[Tool] | None = None,
        tool_choice: Literal["auto", "required", "none"] | None = None,
        max_tool_rounds: int = _MAX_TOOL_ROUNDS_DEFAULT,
        purpose: str | None = None,
    ) -> Conversation[Any]:
        """Send content and parse the final response as structured output."""
        return await self._execute(
            content=content,
            response_format=response_format,
            tools=tools,
            tool_choice=tool_choice,
            max_tool_rounds=max_tool_rounds,
            purpose=purpose,
        )

    @overload
    async def send_spec(
        self,
        spec: PromptSpec[str],
        *,
        documents: Sequence[Document] | None = None,
        include_input_documents: bool = True,
        tools: list[Tool] | None = None,
        tool_choice: Literal["auto", "required", "none"] | None = None,
        max_tool_rounds: int = _MAX_TOOL_ROUNDS_DEFAULT,
        purpose: str | None = None,
    ) -> Conversation[str]: ...

    @overload
    async def send_spec(
        self,
        spec: PromptSpec[U],
        *,
        documents: Sequence[Document] | None = None,
        include_input_documents: bool = True,
        tools: list[Tool] | None = None,
        tool_choice: Literal["auto", "required", "none"] | None = None,
        max_tool_rounds: int = _MAX_TOOL_ROUNDS_DEFAULT,
        purpose: str | None = None,
    ) -> Conversation[U]: ...

    async def send_spec(
        self,
        spec: PromptSpec[Any],
        *,
        documents: Sequence[Document] | None = None,
        include_input_documents: bool = True,
        tools: list[Tool] | None = None,
        tool_choice: Literal["auto", "required", "none"] | None = None,
        max_tool_rounds: int = _MAX_TOOL_ROUNDS_DEFAULT,
        purpose: str | None = None,
    ) -> Conversation[Any]:
        """Render and send a PromptSpec."""
        is_follow_up = spec._follows is not None
        if not is_follow_up and spec.input_documents and not documents and include_input_documents:
            logger.warning(
                "PromptSpec '%s' declares input_documents (%s) but no documents were passed to send_spec(). "
                "Pass documents=[...] or set include_input_documents=False when the context is intentionally absent.",
                spec.__class__.__name__,
                ", ".join(document_type.__name__ for document_type in spec.input_documents),
            )
        if documents and is_follow_up:
            conv: Conversation[Any] = self.with_documents(documents)
        elif documents and include_input_documents:
            conv = self.with_context(*documents)
        else:
            conv = self
        if spec.output_structure is not None:
            options = conv.model_options or ModelOptions()
            if _RESULT_CLOSE not in (options.stop or ()):
                conv = conv.with_model_options(options.model_copy(update={"stop": (*(options.stop or ()), _RESULT_CLOSE)}))
        if ml_messages := _render_multi_line_messages(spec):
            combined = "\n".join(xml_block for _, xml_block in ml_messages)
            conv = conv.model_copy(update={"messages": conv.messages + (UserMessage(combined),)})
        include_docs = bool(spec.input_documents) and documents is not None if is_follow_up else include_input_documents
        prompt_text = render_text(spec, documents=documents, include_input_documents=include_docs)
        trace_purpose = purpose or spec.__class__.__name__
        if spec._output_type is not str:
            return await conv.send_structured(
                prompt_text,
                response_format=spec._output_type,
                tools=tools,
                tool_choice=tool_choice,
                max_tool_rounds=max_tool_rounds,
                purpose=trace_purpose,
            )
        result = await conv.send(
            prompt_text,
            tools=tools,
            tool_choice=tool_choice,
            max_tool_rounds=max_tool_rounds,
            purpose=trace_purpose,
        )
        return result.model_copy(update={"extract_result_tags": True}) if spec.output_structure is not None else result

    def with_document(self, doc: Document) -> Conversation[T]:
        """Return a new Conversation with one dynamic document."""
        return self.model_copy(update={"messages": self.messages + (doc,)})

    def with_documents(self, docs: Sequence[Document] | None) -> Conversation[T]:
        """Return a new Conversation with dynamic documents."""
        return self.model_copy(update={"messages": self.messages + tuple(docs or ())})

    def with_assistant_message(self, content: str) -> Conversation[T]:
        """Return a new Conversation with an assistant message."""
        return self.model_copy(update={"messages": self.messages + (AssistantMessage(content),)})

    def with_context(self, *docs: Document) -> Conversation[T]:
        """Return a new Conversation with cacheable context documents."""
        return self.model_copy(update={"context": self.context + docs})

    def with_model_options(self, options: ModelOptions) -> Conversation[T]:
        """Return a new Conversation with updated public model options."""
        return self.model_copy(update={"model_options": options})

    def with_model(self, model: AIModel) -> Conversation[T]:
        """Return a new Conversation with a different model."""
        return self.model_copy(update={"model": _require_ai_model(model, context="Conversation.with_model()")})

    def with_substitutor(self, enabled: bool = True) -> Conversation[T]:
        """Return a new Conversation with content substitution toggled."""
        return self.model_copy(update={"enable_substitutor": enabled})

    async def _execute(
        self,
        *,
        content: ConversationContent,
        response_format: Any,
        tools: list[Tool] | None,
        tool_choice: Literal["auto", "required", "none"] | None,
        max_tool_rounds: int,
        purpose: str | None,
    ) -> Conversation[Any]:
        validate_send_scope(tools=tools, tool_choice=tool_choice)
        actual_response_format = response_format
        if response_format is not None and is_list_output_type(response_format):
            actual_response_format = ListOf(get_list_item_type(response_format))

        docs = _normalize_content(content)
        new_messages = self.messages + docs
        substitutor = prepare_substitutor(context=self.context, messages=new_messages, enabled=self.enable_substitutor, model=self.model)
        cache_active = self._cache_active_for_turn()
        effective_options = build_effective_options(
            model_options=self.model_options,
            current_date=self.current_date,
            substitutor_active=substitutor is not None and substitutor.pattern_count > 0,
            cache_active=cache_active,
        )
        system_block = effective_options.system_prompt if effective_options is not None else None
        core_messages, context_count = await assemble_api_messages(
            system_block=system_block,
            context=self.context,
            messages=new_messages,
            model=self.model,
            substitutor=substitutor,
        )

        runtime = tool_runtime(tools, tool_choice, max_tool_rounds)
        span_input = self._span_input(content, actual_response_format, tools, tool_choice, max_tool_rounds, purpose)

        request = InteractionRequest(
            messages=tuple(core_messages),
            model=self.model,
            context_count=context_count,
            response_format=actual_response_format,
            tools=runtime,
            substitutor=substitutor,
            purpose=purpose,
            generation_overrides=generation_overrides(effective_options),
            retry_overrides=retry_overrides(effective_options),
            cache_overrides=cache_overrides(effective_options),
            routing_overrides=routing_overrides(
                purpose,
                preferred_deployment_id=self._last_response.transport.aipl.deployment_id if self._last_response is not None else None,
            ),
        )

        execution_ctx = get_execution_context()
        async with track_span(
            SpanKind.CONVERSATION,
            purpose or f"{self.model.name}:{'send_structured' if response_format else 'send'}",
            f"decoded_method:{type(self).__module__}:{type(self).__qualname__}.{'send_structured' if response_format else 'send'}",
            sinks=get_sinks(),
            encode_receiver={"mode": "decoded_state", "value": self},
            encode_input=span_input,
            db=execution_ctx.database if execution_ctx is not None else None,
            input_preview=_core_messages_to_span_input(list(core_messages)),
        ) as span_ctx:
            result = await execute_interaction(request)
            response = result.response
            accumulated = list(result.accumulated_messages)
            if substitutor is not None:
                response = restore_response(response, substitutor, response_format)
                accumulated[-1] = response

            final_messages = new_messages + tuple(accumulated)
            updated = self.model_copy(update={"messages": final_messages})
            updated._tool_call_records = result.tool_call_records
            updated._conversation_id = str(span_ctx.span_id)

            span_ctx.set_meta(
                **self._conversation_meta(
                    request,
                    response,
                    accumulated_messages=tuple(accumulated),
                    llm_round_count=result.llm_round_count,
                    span_input=span_input,
                    effective_options=effective_options,
                )
            )
            span_ctx.set_metrics(
                first_token_ms=int((response.transport.timing.first_token_s or 0) * 1000),
            )
            span_ctx.set_output_preview({"model": response.model, "tool_call_count": len(result.tool_call_records), "content": response.content[:1000]})
            span_ctx._set_output_value(updated)
            return updated

    def _span_input(
        self,
        content: ConversationContent,
        response_format: Any,
        tools: list[Tool] | None,
        tool_choice: str | None,
        max_tool_rounds: int,
        purpose: str | None,
    ) -> dict[str, Any]:
        prompt_content, prompt_document_shas = _prompt_parts(content)
        return {
            "content": content,
            "response_format": response_format,
            "response_format_path": _response_format_path(response_format),
            "tools": tuple(_serialize_tool_config(tool) for tool in tools or ()),
            "tool_choice": tool_choice,
            "max_tool_rounds": max_tool_rounds,
            "purpose": purpose,
            "prompt_content": prompt_content,
            "prompt_document_shas": prompt_document_shas,
        }

    def _conversation_meta(
        self,
        request: InteractionRequest,
        response: ModelResponse[Any],
        *,
        accumulated_messages: tuple[AnyMessage, ...],
        llm_round_count: int,
        span_input: dict[str, Any],
        effective_options: ModelOptions | None,
    ) -> dict[str, Any]:
        runtime = request.tools if isinstance(request.tools, ToolRuntime) else None
        round_responses = tuple(item for item in accumulated_messages if isinstance(item, ModelResponse))
        tried_deployments = _unique(deployment_id for round_response in round_responses for deployment_id in round_response.transport.aipl.tried_deployments)
        failed_deployments = _unique(deployment_id for round_response in round_responses for deployment_id in round_response.transport.aipl.failed_deployments)
        return {
            "purpose": request.purpose,
            "model": response.model,
            "response_content": response.content,
            "reasoning_content": response.reasoning_content,
            "response_id": response.response_id,
            "response_format_path": _response_format_path(request.response_format),
            "effective_system_prompt": effective_options.system_prompt if effective_options is not None else None,
            "model_options": self.model_options.model_dump(mode="json") if self.model_options is not None else None,
            "effective_model_options": effective_options.model_dump(mode="json") if effective_options is not None else None,
            "citations": tuple(citation.model_dump(mode="json") for citation in response.citations),
            "enable_substitutor": self.enable_substitutor,
            "extract_result_tags": self.extract_result_tags,
            "include_date": self.include_date,
            "current_date": self.current_date,
            "prompt_content": span_input.get("prompt_content"),
            "prompt_document_shas": span_input.get("prompt_document_shas"),
            "tools": span_input.get("tools"),
            "tool_schemas": list(runtime.schemas) if runtime is not None else [],
            "tool_choice": getattr(runtime, "choice", None),
            "max_tool_rounds": getattr(runtime, "max_rounds", 0),
            "aipl_summary": {
                "call_id": response.transport.aipl.call_id,
                "deployment_id": response.transport.aipl.deployment_id,
                "provider": response.transport.aipl.provider,
                "group_status": response.transport.aipl.group_status,
                "response_cache_hit": response.transport.aipl.response_cache_hit,
                "round_count": llm_round_count,
                "tool_round_count": sum(1 for round_response in round_responses if round_response.has_tool_calls),
                "tried_deployments": tried_deployments,
                "failed_deployments": failed_deployments,
            },
            "aipl": asdict(response.transport.aipl),
            "litellm": asdict(response.transport.litellm),
            "model_chain": asdict(response.transport.model_chain),
            "prompt_cache_key": response.transport.prompt_cache_key,
            "raw_response_headers": dict(response.transport.raw_response_headers),
        }

    @property
    def approximate_tokens_count(self) -> int:
        """Approximate token count for all context and messages."""
        return approximate_tokens_count(self.context, self.messages)

    def _cache_active_for_turn(self) -> bool:
        """Return True when prompt-cache markers will be applied on this turn.

        Cache markers require both a non-empty cacheable context and a
        positive effective TTL. Used to suppress framework-auto SYSTEM
        injections that would otherwise conflict with Vertex AI's
        cached_content path on Gemini.
        """
        if not self.context:
            return False
        execution_ctx = get_execution_context()
        if execution_ctx is not None and execution_ctx.disable_cache:
            return False
        ttl_override = self.model_options.cache_ttl if self.model_options is not None else None
        effective_ttl = ttl_override if ttl_override is not None else self.model.cache_ttl
        return effective_ttl > 0


def _unique(values: Any) -> tuple[str, ...]:
    seen: dict[str, None] = {}
    for value in values:
        if isinstance(value, str) and value:
            seen.setdefault(value, None)
    return tuple(seen)
