"""Immutable Conversation class for LLM interactions.

Provides a Document-aware, immutable Conversation class that wraps the
primitive _llm_core functions. All operations return NEW Conversation
instances with response properties derived from the last ModelResponse.
"""

import asyncio
import logging
from collections.abc import Mapping, Sequence
from datetime import date
from itertools import chain
from typing import Any, Generic, Literal, Self, TypeVar, cast, overload

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from ai_pipeline_core._llm_core import CoreMessage, ModelOptions, ModelResponse, Role, TokenUsage
from ai_pipeline_core._llm_core import generate as core_generate
from ai_pipeline_core._llm_core import generate_structured as core_generate_structured
from ai_pipeline_core._llm_core.model_response import Citation
from ai_pipeline_core._llm_core.types import ContentPart, TextContent
from ai_pipeline_core._token_estimates import (
    estimate_binary_tokens,
    estimate_image_tokens,
    estimate_message_text_tokens,
    estimate_pdf_tokens,
    estimate_text_tokens,
)
from ai_pipeline_core.database import SpanKind
from ai_pipeline_core.documents import Document
from ai_pipeline_core.pipeline._execution_context import get_execution_context, get_sinks, get_task_context
from ai_pipeline_core.pipeline._track_span import track_span
from ai_pipeline_core.prompt_compiler.render import _RESULT_CLOSE, _extract_result, _render_multi_line_messages, render_text
from ai_pipeline_core.prompt_compiler.spec import PromptSpec

from . import _conversation_messages as _message_helpers
from ._conversation_messages import (
    AnyMessage,
    AssistantMessage,
    ConversationContent,
    ToolResultMessage,
    UserMessage,
    _core_messages_to_db_span_input,
    _core_messages_to_span_input,
    _document_to_content_parts,
    _finish_reason,
    _normalize_content,
    _prompt_parts,
    _response_format_path,
    _serialize_response_tool_calls,
    _serialize_tool_config,
)
from ._list_wrappers import get_list_item_type, get_or_create_wrapper, is_list_output_type, unwrap_list_response
from ._substitutor import URLSubstitutor
from ._tool_loop import execute_tool_loop
from .tools import Tool, ToolCallRecord, ToolOutput, _generate_tool_schema

__all__ = [
    "_LLM_ROUND_REPLAY_TARGET",
    "AssistantMessage",
    "Conversation",
    "ConversationContent",
    "ToolResultMessage",
    "UserMessage",
    "_replay_llm_round",
]

# Instruction appended to system prompt when substitutor is active with patterns
_SUBSTITUTOR_INSTRUCTION = (
    "Text uses ... (three dots) to indicate shortened content. "
    "For example, 0x7a250d56...c659F2488D is a shortened blockchain address, "
    "and https://example.com/very/long/path/to/page...resource.pdf is a shortened URL. "
    "When quoting or referencing such text, preserve entire url or address with the ... markers exactly as shown. "
    "Never create shortened content yourself, you can only reuse existing one."
)

logger = logging.getLogger(__name__)
_escape_xml_content = _message_helpers._escape_xml_content
_escape_xml_metadata = _message_helpers._escape_xml_metadata

# Document name sentinel for system prompt documents — treated as role=SYSTEM in messages
_SYSTEM_PROMPT_DOCUMENT_NAME = "system_prompt"

_CHARS_PER_TOKEN = 4
_MAX_TOOL_ROUNDS_DEFAULT = 10
_LLM_ROUND_REPLAY_TARGET = f"function:{__name__}:_replay_llm_round"

T = TypeVar("T", default=str)
U = TypeVar("U", bound=BaseModel)


class Conversation(BaseModel, Generic[T]):
    """Immutable conversation state for LLM interactions.

    Every send()/send_structured() call returns a NEW Conversation instance.
    Never discard the return value — the original Conversation is unchanged.

    Images in Documents are automatically processed per model preset (splitting, downscaling).

    Content protection (URLs, addresses, high-entropy strings) is enabled by default,
    auto-disabled for `-search` suffix models. Both `.content` and `.parsed` are
    eagerly restored after each send.

    Date awareness: ``include_date=True`` (default) captures the current date at
    construction time and appends ``Current date: YYYY-MM-DD`` to the system prompt.
    The date is frozen at creation and preserved across all builder methods and send()
    calls, ensuring follow-up turns and replays use the same date.

    Attachment rendering in LLM context:
    - Text attachments: wrapped in <attachment name="..." description="..."> tags
    - Binary attachments (images, PDFs): inserted as separate content parts
    """

    model_config = ConfigDict(frozen=True)

    model: str
    context: tuple[Document, ...] = ()
    messages: tuple[AnyMessage, ...] = ()
    model_options: ModelOptions | None = None
    enable_substitutor: bool = True
    extract_result_tags: bool = False
    include_date: bool = True
    current_date: str | None = None
    _conversation_id: str = ""
    _tool_call_records: tuple[ToolCallRecord, ...] = ()

    @model_validator(mode="before")
    @classmethod
    def _disable_substitutor_for_search(cls, data: Any) -> Any:
        """Auto-disable substitutor for search models unless explicitly enabled."""
        if isinstance(data, dict):
            d = cast(dict[str, Any], data)
            if "enable_substitutor" not in d:
                model_name = d.get("model", "")
                if isinstance(model_name, str) and model_name.endswith("-search"):
                    d["enable_substitutor"] = False
        return data  # pyright: ignore[reportUnknownVariableType]

    @model_validator(mode="before")
    @classmethod
    def _initialize_current_date(cls, data: Any) -> Any:
        """Auto-set current_date to today when include_date is True and no date provided."""
        if isinstance(data, dict):
            d = cast(dict[str, Any], data)
            if d.get("include_date", True) and "current_date" not in d:
                d["current_date"] = date.today().isoformat()
        return data  # pyright: ignore[reportUnknownVariableType]

    @field_validator("model")
    @classmethod
    def validate_model_not_empty(cls, v: str) -> str:
        """Reject empty model name."""
        if not v:
            raise ValueError("model must be non-empty")
        return v

    @field_validator("context", "messages", mode="before")
    @classmethod
    def _coerce_to_tuple(cls, v: list[Any] | tuple[Any, ...] | None) -> tuple[Any, ...]:
        """Coerce list or None to immutable tuple."""
        if v is None:
            return ()
        if isinstance(v, list):
            return tuple(v)
        return v

    def __codec_state__(self) -> dict[str, Any]:  # noqa: PLW3201 - Required codec hook name from the replay protocol.
        """Return codec state including Pydantic private fields."""
        conversation_id = cast(str, getattr(self, "_conversation_id", ""))
        tool_call_records = cast(tuple[ToolCallRecord, ...], getattr(self, "_tool_call_records", ()))
        state = {field_name: getattr(self, field_name) for field_name in type(self).model_fields}
        state["messages"] = tuple(_serialize_message_for_codec(message) for message in self.messages)
        state["_conversation_id"] = conversation_id
        state["_tool_call_records"] = tuple(
            {
                "tool": record.tool,
                "input": record.input,
                "output": record.output,
                "round": record.round,
            }
            for record in tool_call_records
        )
        return state

    @classmethod
    def __codec_load__(cls, state: dict[str, Any]) -> Self:  # noqa: PLW3201 - Required codec hook name from the replay protocol.
        """Reconstruct a Conversation from codec state."""
        public_state = {field_name: state[field_name] for field_name in cls.model_fields if field_name in state}
        public_state["messages"] = tuple(_deserialize_message_from_codec(message) for message in public_state.get("messages", ()))
        conversation = cls(**public_state)
        tool_call_records = tuple(
            ToolCallRecord(
                tool=cast(type[Tool], record["tool"]),
                input=cast(BaseModel, record["input"]),
                output=cast(ToolOutput, record["output"]),
                round=cast(int, record["round"]),
            )
            for record in cast(tuple[dict[str, Any], ...], state.get("_tool_call_records", ()))
        )
        object.__setattr__(conversation, "_conversation_id", cast(str, state.get("_conversation_id", "")))
        object.__setattr__(conversation, "_tool_call_records", tool_call_records)
        return conversation

    # --- Response properties (delegate to last ModelResponse) ---

    @property
    def _last_response(self) -> ModelResponse[Any] | None:
        """Get the last ModelResponse from messages."""
        for msg in reversed(self.messages):
            if isinstance(msg, ModelResponse):
                return msg
        return None

    @property
    def content(self) -> str:
        """Response text from last send() call.

        When extract_result_tags is True, strips <result>...</result> tags
        from the raw response (used by send_spec with output_structure).
        """
        if r := self._last_response:
            return _extract_result(r.content) if self.extract_result_tags else r.content
        return ""

    @property
    def reasoning_content(self) -> str:
        """Reasoning content from last send() call (if model supports it)."""
        return r.reasoning_content if (r := self._last_response) else ""

    @property
    def usage(self) -> TokenUsage:
        """Token usage from last send() call."""
        return r.usage if (r := self._last_response) else TokenUsage(prompt_tokens=0, completion_tokens=0, total_tokens=0)

    @property
    def cost(self) -> float | None:
        """Cost from last send() call (if available)."""
        return r.cost if (r := self._last_response) else None

    @property
    def parsed(self) -> T:
        """Parsed result from last send() call.

        For Conversation[str]: returns the content string.
        For Conversation[SomeModel]: returns the typed model instance.
        Raises ValueError if no send() has been called yet.
        """
        r = self._last_response
        if r is None:
            raise ValueError("Cannot access .parsed on a Conversation that has not been sent yet. Call send(), send_structured(), or send_spec() first.")
        return r.parsed  # type: ignore[return-value] — ModelResponse stored as ModelResponse[Any] in heterogeneous messages tuple loses generic T

    @property
    def citations(self) -> tuple[Citation, ...]:
        """Citations from last send() call (for search-enabled models)."""
        return tuple(r.citations) if (r := self._last_response) else ()

    @property
    def tool_call_records(self) -> tuple[ToolCallRecord, ...]:
        """All tool call records from this Conversation's send() call (across all rounds)."""
        return self._tool_call_records

    def tool_calls_for(self, tool_cls: type[Tool]) -> tuple[ToolCallRecord, ...]:
        """Filter tool call records by tool class.

        Returns only records from this Conversation's send() call where the tool
        matches the given class. Use with the collection pattern across phases:

            all = phase1.tool_calls_for(Inspect) + phase2.tool_calls_for(Inspect)
        """
        return tuple(r for r in self._tool_call_records if r.tool is tool_cls)

    def _restore_content(self, text: str) -> str:
        """Restore shortened URLs/addresses in text using the substitutor from the last send.

        Use when extracting URLs from .parsed structured output fields, as .parsed may
        contain shortened forms.
        """
        # Substitutor is ephemeral — not stored on Conversation. For restore_content
        # to work, the caller needs the same conv that produced the response.
        # Since we can't store the substitutor (not serializable), we reconstruct it.
        if not self.enable_substitutor:
            return text
        substitutor = URLSubstitutor()
        all_items = self.context + tuple(m for m in self.messages if isinstance(m, (Document, UserMessage, AssistantMessage, ToolResultMessage)))
        substitutor.prepare(self._collect_text(all_items))
        return substitutor.restore(text)

    # --- Core message conversion ---

    def _to_core_messages(self, items: tuple[AnyMessage, ...]) -> list[CoreMessage]:
        """Convert Documents, UserMessages, AssistantMessages, ToolResultMessages, and ModelResponses to CoreMessages."""
        core_messages: list[CoreMessage] = []

        for item in items:
            if isinstance(item, ModelResponse):
                if item.has_tool_calls:
                    core_messages.append(CoreMessage(role=Role.ASSISTANT, content=item.content or "", tool_calls=item.tool_calls))
                else:
                    core_messages.append(CoreMessage(role=Role.ASSISTANT, content=item.content))
            elif isinstance(item, ToolResultMessage):
                core_messages.append(CoreMessage(role=Role.TOOL, content=item.content, tool_call_id=item.tool_call_id, name=item.function_name))
            elif isinstance(item, AssistantMessage):
                core_messages.append(CoreMessage(role=Role.ASSISTANT, content=item.text))
            elif isinstance(item, UserMessage):
                core_messages.append(CoreMessage(role=Role.USER, content=item.text))
            elif isinstance(item, Document):  # pyright: ignore[reportUnnecessaryIsInstance]
                if item.name == _SYSTEM_PROMPT_DOCUMENT_NAME:
                    core_messages.append(CoreMessage(role=Role.SYSTEM, content=item.text))
                else:
                    parts = _document_to_content_parts(item, self.model)
                    if parts:
                        if len(parts) == 1 and isinstance(parts[0], TextContent):
                            core_messages.append(CoreMessage(role=Role.USER, content=parts[0].text))
                        else:
                            core_messages.append(CoreMessage(role=Role.USER, content=tuple(parts)))

        return core_messages

    # --- Substitution ---

    @staticmethod
    def _collect_text(items: tuple[AnyMessage, ...]) -> list[str]:
        """Collect text content from documents and messages for substitutor preparation."""
        texts: list[str] = []
        for item in items:
            if isinstance(item, (UserMessage, AssistantMessage)) or (isinstance(item, Document) and item.is_text):
                texts.append(item.text)
            elif isinstance(item, ToolResultMessage):
                texts.append(item.content)
        return texts

    @staticmethod
    def _apply_substitution(core_messages: list[CoreMessage], substitutor: URLSubstitutor) -> list[CoreMessage]:
        """Apply URL/address substitution to text content in messages."""
        result: list[CoreMessage] = []
        for msg in core_messages:
            if isinstance(msg.content, str):
                result.append(
                    CoreMessage(
                        role=msg.role,
                        content=substitutor.substitute(msg.content),
                        tool_calls=msg.tool_calls,
                        tool_call_id=msg.tool_call_id,
                        name=msg.name,
                    )
                )
            elif isinstance(msg.content, tuple):
                new_parts: list[ContentPart] = []
                for part in msg.content:
                    if isinstance(part, TextContent):
                        new_parts.append(TextContent(text=substitutor.substitute(part.text)))
                    else:
                        new_parts.append(part)
                result.append(
                    CoreMessage(
                        role=msg.role,
                        content=tuple(new_parts),
                        tool_calls=msg.tool_calls,
                        tool_call_id=msg.tool_call_id,
                        name=msg.name,
                    )
                )
            else:
                result.append(msg)
        return result

    @staticmethod
    def _restore_response(response: ModelResponse[Any], substitutor: URLSubstitutor, response_format: Any = None) -> ModelResponse[Any]:
        """Restore shortened URLs/addresses in LLM response.

        For list[BaseModel] output types, re-parses the array JSON with TypeAdapter.
        """
        if substitutor.pattern_count == 0:
            return response
        restored = substitutor.restore(response.content)
        if restored == response.content:
            return response
        update: dict[str, Any] = {"content": restored}
        if response_format is not None and not isinstance(response.parsed, str):
            try:
                if is_list_output_type(response_format):
                    from pydantic import TypeAdapter

                    update["parsed"] = TypeAdapter(response_format).validate_json(restored)
                else:
                    update["parsed"] = response_format.model_validate_json(restored)
            except Exception:
                format_name = getattr(response_format, "__name__", str(response_format))
                logger.warning("URL-restored content failed structured re-parse for %s; falling back to plain text.", format_name)
                update["parsed"] = restored
        else:
            update["parsed"] = restored
        return response.model_copy(update=update)  # nosemgrep: no-document-model-copy

    @staticmethod
    def _metadata_float(metadata: Mapping[str, Any], key: str) -> float:
        """Read numeric timing metadata defensively, defaulting missing or invalid values to zero."""
        raw_value = metadata.get(key, 0.0)
        try:
            return float(raw_value)
        except TypeError, ValueError:
            return 0.0

    # --- Send methods ---

    @staticmethod
    def _build_effective_options(
        base: ModelOptions | None,
        *,
        current_date: str | None,
        substitutor_active: bool,
    ) -> ModelOptions | None:
        """Build effective ModelOptions by appending date and substitutor instructions to system prompt."""
        effective = base
        for extra in (
            f"Current date: {current_date}" if current_date else None,
            _SUBSTITUTOR_INSTRUCTION if substitutor_active else None,
        ):
            if extra:
                base_prompt = effective.system_prompt if effective else None
                combined = f"{base_prompt}\n\n{extra}" if base_prompt else extra
                effective = (effective or ModelOptions()).model_copy(update={"system_prompt": combined})  # nosemgrep: no-document-model-copy
        execution_ctx = get_execution_context()
        if execution_ctx is not None and execution_ctx.disable_cache:
            effective = (effective or ModelOptions()).model_copy(update={"cache_ttl": None})
        return effective

    async def _invoke_llm(
        self,
        *,
        core_messages: list[CoreMessage],
        effective_options: ModelOptions | None,
        context_count: int,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | None = None,
        response_format: type[BaseModel] | None = None,
        purpose: str | None = None,
        expected_cost: float | None = None,
        round_index: int = 1,
        tool_schemas: list[dict[str, Any]] | None = None,
        _list_item_type: type[BaseModel] | None = None,
    ) -> ModelResponse[Any]:
        """Single LLM call wrapper — used directly and by the tool loop.

        When ``_list_item_type`` is set, the span stores the item type (importable)
        with a ``_response_format_is_list`` flag instead of the wrapper type, so that
        replay can reconstruct the wrapper without codec changes.
        """
        # For observability, show the declared output type (list[T] or T), not the wrapper
        declared_format: Any = list[_list_item_type] if _list_item_type else response_format
        response_format_path = _response_format_path(declared_format) or None
        span_input_preview = _core_messages_to_span_input(core_messages)
        span_input_db = _core_messages_to_db_span_input(core_messages)
        if effective_options and effective_options.system_prompt:
            system_entry: dict[str, Any] = {"role": "system", "content": effective_options.system_prompt}
            span_input_preview = [system_entry] + span_input_preview
            span_input_db = [system_entry] + span_input_db
        execution_ctx = get_execution_context()
        llm_target = _LLM_ROUND_REPLAY_TARGET
        llm_input: dict[str, Any] = {
            "messages": core_messages,
            "model": self.model,
            "model_options": effective_options,
            "tool_choice": tool_choice,
            "response_format": _list_item_type if _list_item_type else response_format,
            "_response_format_is_list": _list_item_type is not None,
            "purpose": purpose,
            "expected_cost": expected_cost,
            "round_index": round_index,
            "tool_schemas": tool_schemas or [],
        }

        async with track_span(
            SpanKind.LLM_ROUND,
            f"round-{round_index}",
            llm_target,
            sinks=get_sinks(),
            db=execution_ctx.database if execution_ctx is not None else None,
            encode_input=llm_input,
            input_preview=span_input_preview,
        ) as span_ctx:
            try:
                if response_format is not None:
                    response = await core_generate_structured(
                        core_messages,
                        response_format,
                        model=self.model,
                        model_options=effective_options,
                        purpose=purpose,
                        expected_cost=expected_cost,
                        context_count=context_count,
                        tools=tools,
                        tool_choice=tool_choice,
                    )
                else:
                    response = await core_generate(
                        core_messages,
                        model=self.model,
                        model_options=effective_options,
                        purpose=purpose,
                        expected_cost=expected_cost,
                        context_count=context_count,
                        tools=tools,
                        tool_choice=tool_choice,
                    )
            except (Exception, asyncio.CancelledError) as exc:
                span_ctx.set_meta(
                    model=self.model,
                    finish_reason="error",
                    response_id="",
                    tool_schemas=list(tool_schemas or []),
                    round_index=round_index,
                    request_messages=span_input_db,
                    response_content="",
                    response_tool_calls=[],
                    response_format_path=response_format_path,
                    tool_call_count=0,
                    tool_choice=tool_choice,
                    error_type=type(exc).__name__,
                    error_message=str(exc),
                )
                raise
            span_ctx.set_meta(
                model=response.model or self.model,
                finish_reason=_finish_reason(response),
                response_id=response.response_id,
                tool_schemas=list(tool_schemas or []),
                round_index=round_index,
                request_messages=span_input_db,
                response_content=response.content,
                response_tool_calls=_serialize_response_tool_calls(response.tool_calls),
                response_format_path=response_format_path,
                tool_call_count=len(response.tool_calls),
                tool_choice=tool_choice,
            )
            span_ctx.set_metrics(
                tokens_input=response.usage.prompt_tokens,
                tokens_output=response.usage.completion_tokens,
                tokens_cache_read=response.usage.cached_tokens,
                tokens_reasoning=response.usage.reasoning_tokens,
                cost_usd=response.cost or 0.0,
                first_token_ms=int(self._metadata_float(response.metadata, "first_token_time") * 1000),
            )
            outputs = [item for item in (response.reasoning_content, response.content) if item]
            span_ctx.set_output_preview(outputs if len(outputs) > 1 else response.content)
            span_ctx._set_output_value(response)
            return response

    async def _execute_send(
        self,
        content: ConversationContent,
        response_format: Any,
        purpose: str | None,
        expected_cost: float | None,
        tools: list[Tool] | None = None,
        tool_choice: Literal["auto", "required", "none"] | None = None,
        max_tool_rounds: int = _MAX_TOOL_ROUNDS_DEFAULT,
    ) -> tuple[tuple[AnyMessage, ...], ModelResponse[Any], tuple[ToolCallRecord, ...], str]:
        """Common preparation, LLM call (or tool loop), and response restoration.

        ``response_format`` accepts ``type[BaseModel]``, ``list[type[BaseModel]]``, or ``None``.
        For list types, wraps in a single-field object model before the LLM call and unwraps after.
        """
        task_ctx = get_task_context()
        if task_ctx is not None and task_ctx.scope_kind == "flow":
            raise RuntimeError(
                "Conversation.send()/send_structured()/send_spec() called from flow scope without a task. "
                "LLM calls must happen inside a PipelineTask, not directly in PipelineFlow.run(). "
                "Create a task for the LLM interaction and call it from the flow."
            )
        if tool_choice is not None and not tools:
            raise ValueError(f"tool_choice='{tool_choice}' requires tools= to be provided. Pass a list of Tool instances with tools=[...].")

        # Detect list output type and resolve wrapper
        list_item_type: type[BaseModel] | None = None
        actual_response_format: type[BaseModel] | None = response_format
        if response_format is not None and is_list_output_type(response_format):
            list_item_type = get_list_item_type(response_format)
            actual_response_format = get_or_create_wrapper(list_item_type)

        docs = _normalize_content(content)
        new_messages = self.messages + docs

        # Prepare substitutor fresh each call — no mutable state stored on Conversation
        substitutor: URLSubstitutor | None = None
        if self.enable_substitutor:
            substitutor = URLSubstitutor()
            all_items = self.context + tuple(m for m in new_messages if isinstance(m, (Document, UserMessage, AssistantMessage, ToolResultMessage)))
            substitutor.prepare(self._collect_text(all_items))

        # Build effective options — append current date and substitutor instructions to system prompt
        effective_options = self._build_effective_options(
            self.model_options,
            current_date=self.current_date,
            substitutor_active=substitutor is not None and substitutor.pattern_count > 0,
        )

        # Build CoreMessages in thread (CPU-bound image/PDF processing)
        context_core, messages_core = await asyncio.to_thread(lambda: (self._to_core_messages(self.context), self._to_core_messages(new_messages)))
        core_messages = context_core + messages_core
        context_count = len(context_core)

        # Prepare tool schemas and lookup
        tool_schemas: list[dict[str, Any]] | None = None
        tool_lookup: dict[str, Tool] | None = None
        if tools:
            tool_schemas = [_generate_tool_schema(t) for t in tools]
            tool_lookup = {}
            for t in tools:
                name = type(t).name
                if name in tool_lookup:
                    raise ValueError(f"Duplicate tool name '{name}'. Tool names must be unique after snake_case conversion.")
                tool_lookup[name] = t

        recorder_name = purpose or f"{self.model}:{'send_structured' if response_format else 'send'}"

        tool_records: tuple[ToolCallRecord, ...] = ()
        accumulated_tool_messages: list[Any] = []
        model_options = self.model_options.model_dump(mode="json") if self.model_options else {}
        effective_model_options = effective_options.model_dump(mode="json") if effective_options else {}
        effective_system_prompt = effective_options.system_prompt if effective_options is not None else None
        response_format_path = _response_format_path(response_format)
        prompt_content, prompt_document_shas = _prompt_parts(content)
        serialized_tools = tuple(_serialize_tool_config(tool) for tool in tools or [])

        # Build span input from pre-substitution messages (shows full context with binary placeholders)
        span_input = _core_messages_to_span_input(core_messages)
        conversation_target = f"decoded_method:{type(self).__module__}:{type(self).__qualname__}.{'send_structured' if response_format else 'send'}"
        conversation_input: dict[str, Any] = {
            "content": content,
            "response_format": list_item_type if list_item_type else actual_response_format,
            "_response_format_is_list": list_item_type is not None,
            "tools": serialized_tools,
            "tool_choice": tool_choice,
            "max_tool_rounds": max_tool_rounds,
            "purpose": purpose,
            "expected_cost": expected_cost,
        }
        execution_ctx = get_execution_context()

        async with track_span(
            SpanKind.CONVERSATION,
            recorder_name,
            conversation_target,
            sinks=get_sinks(),
            encode_receiver={"mode": "decoded_state", "value": self},
            encode_input=conversation_input,
            db=execution_ctx.database if execution_ctx is not None else None,
            input_preview=span_input,
        ) as span_ctx:
            if substitutor:
                core_messages = self._apply_substitution(core_messages, substitutor)
            try:
                if tools and tool_schemas and tool_lookup:
                    accumulated_tool_messages, response, tool_records = await execute_tool_loop(
                        invoke_llm=self._invoke_llm,
                        tool_schemas=tool_schemas,
                        tool_lookup=tool_lookup,
                        tool_choice=tool_choice or "auto",
                        max_tool_rounds=max_tool_rounds,
                        purpose=purpose,
                        expected_cost=expected_cost,
                        core_messages=core_messages,
                        context_count=context_count,
                        effective_options=effective_options,
                        substitutor=substitutor,
                        build_tool_result_message=lambda tid, fn, c: ToolResultMessage(tool_call_id=tid, function_name=fn, content=c),
                        response_format=actual_response_format,
                        _list_item_type=list_item_type,
                    )
                else:
                    response = await self._invoke_llm(
                        core_messages=core_messages,
                        effective_options=effective_options,
                        context_count=context_count,
                        response_format=actual_response_format,
                        purpose=purpose,
                        expected_cost=expected_cost,
                        round_index=1,
                        tool_schemas=tool_schemas,
                        _list_item_type=list_item_type,
                    )
            except (Exception, asyncio.CancelledError) as exc:
                span_ctx.set_meta(
                    purpose=recorder_name,
                    citations=[],
                    enable_substitutor=self.enable_substitutor,
                    extract_result_tags=self.extract_result_tags,
                    include_date=self.include_date,
                    current_date=self.current_date,
                    model=self.model,
                    prompt_content=prompt_content,
                    prompt_document_shas=prompt_document_shas,
                    model_options=model_options,
                    effective_model_options=effective_model_options,
                    effective_system_prompt=effective_system_prompt,
                    response_format_path=response_format_path,
                    tools=serialized_tools,
                    tool_choice=tool_choice,
                    max_tool_rounds=max_tool_rounds,
                    error_type=type(exc).__name__,
                    error_message=str(exc),
                )
                raise
            # Unwrap list response FIRST: extract list from wrapper, rewrite content as JSON array.
            # Must happen before URL restoration so .content is array JSON when TypeAdapter parses it.
            if list_item_type is not None and not response.has_tool_calls:
                response = unwrap_list_response(response, list_item_type)
                if accumulated_tool_messages:
                    accumulated_tool_messages[-1] = response

            if substitutor:
                response = self._restore_response(response, substitutor, response_format)
                if accumulated_tool_messages:
                    accumulated_tool_messages[-1] = response

            if accumulated_tool_messages:
                final_messages = new_messages + tuple(accumulated_tool_messages)
            else:
                final_messages = new_messages + (response,)

            new_conversation_id = str(span_ctx.span_id)
            updated_conversation = self.model_copy(
                update={
                    "messages": final_messages,
                    "_tool_call_records": tool_records,
                    "_conversation_id": new_conversation_id,
                }
            )
            span_ctx.set_meta(
                purpose=recorder_name,
                citations=[{"title": c.title, "url": c.url, "start_index": c.start_index, "end_index": c.end_index} for c in response.citations],
                enable_substitutor=self.enable_substitutor,
                extract_result_tags=self.extract_result_tags,
                include_date=self.include_date,
                current_date=self.current_date,
                model=response.model or self.model,
                prompt_content=prompt_content,
                prompt_document_shas=prompt_document_shas,
                response_content=response.content,
                reasoning_content=response.reasoning_content or "",
                response_id=response.response_id,
                model_options=model_options,
                effective_model_options=effective_model_options,
                effective_system_prompt=effective_system_prompt,
                response_format_path=response_format_path,
                tools=serialized_tools,
                tool_choice=tool_choice,
                max_tool_rounds=max_tool_rounds,
            )
            span_ctx.set_metrics(
                first_token_ms=int(self._metadata_float(response.metadata, "first_token_time") * 1000),
            )
            span_ctx.set_output_preview({"model": response.model or self.model, "tool_call_count": len(tool_records)})
            span_ctx._set_output_value(updated_conversation)
            return final_messages, response, tool_records, new_conversation_id

    async def send(
        self,
        content: ConversationContent,
        *,
        tools: list[Tool] | None = None,
        tool_choice: Literal["auto", "required", "none"] | None = None,
        max_tool_rounds: int = _MAX_TOOL_ROUNDS_DEFAULT,
        purpose: str | None = None,
        expected_cost: float | None = None,
    ) -> Conversation[str]:
        """Send message, returns NEW Conversation with response.

        Document content is wrapped in <document> XML tags with id, name, description.
        When tools are provided, runs an auto-loop: LLM → tool calls → execute → re-send.
        """
        new_messages, _response, records, new_conversation_id = await self._execute_send(
            content,
            None,
            purpose,
            expected_cost,
            tools=tools,
            tool_choice=tool_choice,
            max_tool_rounds=max_tool_rounds,
        )
        return self.model_copy(update={"messages": new_messages, "_tool_call_records": records, "_conversation_id": new_conversation_id})  # type: ignore[return-value]

    async def send_structured(
        self,
        content: ConversationContent,
        response_format: Any,
        *,
        tools: list[Tool] | None = None,
        tool_choice: Literal["auto", "required", "none"] | None = None,
        max_tool_rounds: int = _MAX_TOOL_ROUNDS_DEFAULT,
        purpose: str | None = None,
        expected_cost: float | None = None,
    ) -> Conversation[Any]:
        """Send message expecting structured response, returns NEW Conversation with .parsed.

        ``response_format`` accepts ``type[BaseModel]`` for single-model output or
        ``list[type[BaseModel]]`` for list output. For list types, the framework
        wraps the schema in a single-field object (required by all LLM providers)
        and unwraps the response transparently.

        Quality degrades beyond ~2-3K output tokens or nesting >2 levels.
        Never use dict types in response_format — use lists of typed models.
        Split complex structures across multiple calls.

        When tools are provided, response_format is passed on every tool-loop round.
        Structured parsing occurs on the final response (when the LLM stops calling tools).
        """
        new_messages, _response, records, new_conversation_id = await self._execute_send(
            content,
            response_format,
            purpose,
            expected_cost,
            tools=tools,
            tool_choice=tool_choice,
            max_tool_rounds=max_tool_rounds,
        )
        return self.model_copy(update={"messages": new_messages, "_tool_call_records": records, "_conversation_id": new_conversation_id})

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
        expected_cost: float | None = None,
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
        expected_cost: float | None = None,
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
        expected_cost: float | None = None,
    ) -> Conversation[Any]:
        """Send a PromptSpec to the LLM.

        Adds documents to context (or messages for follow-up specs), renders the
        prompt, and dispatches to send() or send_structured() based on output_type.

        For specs with output_structure, sets stop sequences at </result> and
        auto-extracts content — conv.content returns clean text.

        For follow-up specs (follows is set), documents go to messages instead of
        context. If the follow-up declares input_documents and documents are passed,
        they are listed in the prompt text with their runtime id, name, description.
        """
        is_follow_up = spec._follows is not None

        # Warning for missing documents (only for non-follow-up specs)
        if not is_follow_up and spec.input_documents and not documents and include_input_documents:
            logger.warning(
                "PromptSpec '%s' declares input_documents (%s) but no documents were passed to send_spec().",
                spec.__class__.__name__,
                ", ".join(d.__name__ for d in spec.input_documents),
            )

        # Place documents in context (initial specs) or messages (follow-ups)
        if documents:
            if is_follow_up:
                conv = self.with_documents(documents)
            else:
                conv = self.with_context(*documents)
        else:
            conv = self

        # Set stop sequence when output_structure is set (result tag wrapping)
        if spec.output_structure is not None:
            opts = conv.model_options or ModelOptions()
            if _RESULT_CLOSE not in (opts.stop or ()):
                stop = (*opts.stop, _RESULT_CLOSE) if opts.stop else (_RESULT_CLOSE,)
                conv = conv.with_model_options(opts.model_copy(update={"stop": stop}))  # nosemgrep: no-document-model-copy

        # Determine whether to include input documents in prompt text
        # Follow-ups: only include if the spec declares its own input_documents AND documents are passed
        if is_follow_up:
            effective_include_docs = bool(spec.input_documents) and documents is not None
        else:
            effective_include_docs = include_input_documents

        # Add multi-line field values as a single user message before the prompt
        ml_messages = _render_multi_line_messages(spec)
        if ml_messages:
            combined = "\n".join(xml_block for _, xml_block in ml_messages)
            conv = conv.model_copy(update={"messages": conv.messages + (UserMessage(combined),)})

        prompt_text = render_text(spec, documents=documents, include_input_documents=effective_include_docs)
        trace_purpose = purpose or spec.__class__.__name__

        # Dispatch to structured or text generation
        if spec._output_type is not str:
            return await conv.send_structured(
                prompt_text,
                response_format=spec._output_type,
                tools=tools,
                tool_choice=tool_choice,
                max_tool_rounds=max_tool_rounds,
                purpose=trace_purpose,
                expected_cost=expected_cost,
            )

        result = await conv.send(
            prompt_text,
            tools=tools,
            tool_choice=tool_choice,
            max_tool_rounds=max_tool_rounds,
            purpose=trace_purpose,
            expected_cost=expected_cost,
        )

        if spec.output_structure is not None:
            return result.model_copy(update={"extract_result_tags": True})

        return result

    # --- Builder methods (return NEW Conversation) ---

    def with_document(self, doc: Document) -> Conversation[T]:
        """Return NEW Conversation with document appended to messages (dynamic suffix, not cached)."""
        return self.model_copy(update={"messages": self.messages + (doc,)})

    def with_documents(self, docs: Sequence[Document]) -> Conversation[T]:
        """Return NEW Conversation with multiple documents appended to messages (not cached)."""
        return self.model_copy(update={"messages": self.messages + tuple(docs)})

    def with_assistant_message(self, content: str) -> Conversation[T]:
        """Return NEW Conversation with an injected assistant turn in messages."""
        return self.model_copy(update={"messages": self.messages + (AssistantMessage(content),)})

    def with_context(self, *docs: Document) -> Conversation[T]:
        """Return NEW Conversation with documents added to the cacheable context prefix.

        Always set context before the first send() — adding context mid-conversation
        changes the prefix, so subsequent send() calls will not hit the cache from prior configurations.
        """
        return self.model_copy(update={"context": self.context + docs})

    def with_model_options(self, options: ModelOptions) -> Conversation[T]:
        """Return NEW Conversation with updated model options."""
        return self.model_copy(update={"model_options": options})

    def with_model(self, model: str) -> Conversation[T]:
        """Return NEW Conversation with a different model, preserving all state."""
        if not model:
            raise ValueError("model must be non-empty")
        return self.model_copy(update={"model": model})

    def with_substitutor(self, enabled: bool = True) -> Conversation[T]:
        """Return NEW Conversation with content protection enabled/disabled.

        Shortens URLs, blockchain addresses, and high-entropy strings before sending.
        Both .content and .parsed are eagerly restored after each send.
        Auto-disabled for -search suffix models.
        """
        return self.model_copy(update={"enable_substitutor": enabled})

    # --- Utilities ---

    @property
    def approximate_tokens_count(self) -> int:
        """Approximate token count for all context and messages."""
        total = 0
        for item in chain(self.context, self.messages):
            if isinstance(item, ModelResponse):
                total += estimate_message_text_tokens(item.content)
                if reasoning := item.reasoning_content:
                    total += estimate_message_text_tokens(reasoning)
            elif isinstance(item, ToolResultMessage):
                total += estimate_message_text_tokens(item.content)
            elif isinstance(item, (UserMessage, AssistantMessage)):
                total += estimate_message_text_tokens(item.text)
            elif isinstance(item, Document):  # pyright: ignore[reportUnnecessaryIsInstance]
                if item.is_text:
                    total += estimate_text_tokens(item.text)
                elif item.is_image:
                    total += estimate_image_tokens()
                elif item.is_pdf:
                    total += estimate_pdf_tokens()
                else:
                    total += estimate_binary_tokens()
                for att in item.attachments:
                    if att.is_text:
                        total += estimate_text_tokens(att.text)
                    elif att.is_image:
                        total += estimate_image_tokens()
                    elif att.is_pdf:
                        total += estimate_pdf_tokens()
                    else:
                        total += estimate_binary_tokens()
        return total


async def _replay_llm_round(
    *,
    messages: list[CoreMessage],
    model: str,
    model_options: ModelOptions | None = None,
    tool_choice: str | None = None,
    response_format: type[BaseModel] | None = None,
    _response_format_is_list: bool = False,
    purpose: str | None = None,
    expected_cost: float | None = None,
    round_index: int = 1,
    tool_schemas: list[dict[str, Any]] | None = None,
) -> ModelResponse[Any]:
    """Replay one recorded llm_round boundary against the primitive LLM client.

    When ``_response_format_is_list`` is True, ``response_format`` is the item type
    and a wrapper model is reconstructed for the actual LLM call.
    """
    _ = round_index
    actual_format = response_format
    if _response_format_is_list and response_format is not None:
        actual_format = get_or_create_wrapper(response_format)
    if actual_format is not None:
        result = await core_generate_structured(
            messages,
            actual_format,
            model=model,
            model_options=model_options,
            purpose=purpose,
            expected_cost=expected_cost,
            context_count=0,
            tools=tool_schemas,
            tool_choice=tool_choice,
        )
        if _response_format_is_list and response_format is not None:
            result = unwrap_list_response(result, response_format)
        return result
    return await core_generate(
        messages,
        model=model,
        model_options=model_options,
        purpose=purpose,
        expected_cost=expected_cost,
        context_count=0,
        tools=tool_schemas,
        tool_choice=tool_choice,
    )


def _serialize_message_for_codec(message: AnyMessage) -> Any:
    if isinstance(message, UserMessage):
        return {"__conversation_message__": "user", "text": message.text}
    if isinstance(message, AssistantMessage):
        return {"__conversation_message__": "assistant", "text": message.text}
    if isinstance(message, ToolResultMessage):
        return {
            "__conversation_message__": "tool_result",
            "tool_call_id": message.tool_call_id,
            "function_name": message.function_name,
            "content": message.content,
        }
    return message


def _deserialize_message_from_codec(message: Any) -> AnyMessage:
    if not isinstance(message, dict):
        return cast(AnyMessage, message)
    message_kind = message.get("__conversation_message__")
    if message_kind == "user":
        return UserMessage(text=cast(str, message["text"]))
    if message_kind == "assistant":
        return AssistantMessage(text=cast(str, message["text"]))
    if message_kind == "tool_result":
        return ToolResultMessage(
            tool_call_id=cast(str, message["tool_call_id"]),
            function_name=cast(str, message["function_name"]),
            content=cast(str, message["content"]),
        )
    return cast(AnyMessage, message)
