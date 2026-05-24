"""Streaming accumulation and session management for LLM responses."""

import asyncio
import contextlib
import json
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from inspect import isawaitable
from types import SimpleNamespace
from typing import Any

from ._aipl import AIPLResponseHeaders
from ._watchdog import StreamWatchdog
from .exceptions import MidStreamProviderError, PartialToolCallStreamError, StreamWatchdogError
from .model_response import StreamCompletion, TimingData

__all__ = ["StreamAccumulator", "StreamSession"]


@dataclass(slots=True)
class _ToolCallParts:
    """Partial tool call assembled from streamed deltas."""

    id: str | None = None
    name: str | None = None
    arguments: str = ""
    provider_specific_fields: dict[str, Any] | None = None


@dataclass(slots=True)
class StreamAccumulator:
    """Accumulate ChatCompletionChunk deltas into a response-like object."""

    response_id: str = ""
    role: str | None = None
    content: str = ""
    reasoning_content: str = ""
    finish_reason: str | None = None
    annotations: list[Any] = field(default_factory=list)
    thinking_blocks: list[Any] = field(default_factory=list)
    provider_specific_fields: dict[str, Any] = field(default_factory=dict)
    tool_calls: dict[int, _ToolCallParts] = field(default_factory=dict)
    usage: Any = None
    last_content_tokens: int = 0
    last_tool_call_tokens: int = 0
    last_reasoning_tokens: int = 0
    saw_content_chunk: bool = False

    def add_chunk(self, chunk: Any) -> None:
        """Fold one streaming chunk into the accumulator.

        ``saw_content_chunk`` tracks only chunks that carry committed model
        output (text, reasoning text, or a tool-call delta). Protocol-level
        events such as Responses' ``response.created`` arrive before any
        usable content; classifying a drop after those as
        ``MidStreamProviderError`` would mask retryable transport failures
        as terminal stream errors.

        ``last_tool_call_tokens`` is the subset of ``last_content_tokens``
        attributable to tool-call deltas, so the watchdog can apply its
        relaxed inactivity gate while a tool call is in progress.
        """
        self.last_content_tokens = 0
        self.last_tool_call_tokens = 0
        self.last_reasoning_tokens = 0
        chunk_id = getattr(chunk, "id", None)
        if chunk_id and not self.response_id:
            self.response_id = str(chunk_id)
        chunk_usage = getattr(chunk, "usage", None)
        if chunk_usage is not None:
            self.usage = chunk_usage
        choices = getattr(chunk, "choices", None) or []
        if not choices:
            return
        choice = choices[0]
        finish = getattr(choice, "finish_reason", None)
        if finish:
            self.finish_reason = str(finish)
        delta = getattr(choice, "delta", None)
        if delta is not None:
            role = getattr(delta, "role", None)
            if role and self.role is None:
                self.role = str(role)
            self._absorb_content(delta)
            self._absorb_reasoning(delta)
            self._absorb_tool_calls(delta)
            self._absorb_provider_fields(delta)
            delta_annotations = getattr(delta, "annotations", None)
            if delta_annotations:
                self.annotations.extend(delta_annotations)
        message = getattr(choice, "message", None)
        if message is not None:
            annotations = getattr(message, "annotations", None)
            if annotations:
                self.annotations.extend(annotations)
            fields = getattr(message, "provider_specific_fields", None)
            if isinstance(fields, Mapping):
                self._merge_provider_fields(fields)
            reasoning_items = getattr(message, "reasoning_items", None)
            if reasoning_items:
                self._merge_value_into(self.provider_specific_fields, "reasoning_items", list(reasoning_items))

    def build_response(self) -> Any:
        """Build a response-like object compatible with response extraction."""
        tool_calls = [
            SimpleNamespace(
                id=parts.id,
                type="function",
                function=SimpleNamespace(name=parts.name or "", arguments=parts.arguments or "{}"),
                provider_specific_fields=parts.provider_specific_fields,
            )
            for _, parts in sorted(self.tool_calls.items())
        ]
        message = SimpleNamespace(
            role=self.role or "assistant",
            content=self.content,
            reasoning_content=self.reasoning_content or None,
            tool_calls=tool_calls or None,
            annotations=self.annotations or None,
            thinking_blocks=tuple(self.thinking_blocks) if self.thinking_blocks else None,
            provider_specific_fields=dict(self.provider_specific_fields) if self.provider_specific_fields else None,
        )
        choice = SimpleNamespace(index=0, message=message, finish_reason=self.finish_reason or "stop")
        return SimpleNamespace(id=self.response_id, choices=[choice], usage=self.usage)

    def _absorb_content(self, delta: Any) -> None:
        content = getattr(delta, "content", None)
        if isinstance(content, str) and content:
            self.content += content
            self.last_content_tokens += _estimate_tokens(content)
            self.saw_content_chunk = True

    def _absorb_reasoning(self, delta: Any) -> None:
        details = getattr(delta, "reasoning_details", None)
        if details:
            extracted = "".join(
                str(getattr(item, "text", "") or (item.get("text") if isinstance(item, Mapping) else "") or "")
                for item in details
            )
            self.provider_specific_fields.setdefault("reasoning_details", [])
            existing = self.provider_specific_fields["reasoning_details"]
            if isinstance(existing, list):
                existing.extend(details)
            if extracted:
                self.reasoning_content += extracted
                self.last_reasoning_tokens += _estimate_tokens(extracted)
                self.saw_content_chunk = True
            return
        for attr in ("reasoning_content", "reasoning"):
            value = getattr(delta, attr, None)
            if isinstance(value, str) and value:
                self.reasoning_content += value
                self.last_reasoning_tokens += _estimate_tokens(value)
                self.saw_content_chunk = True
                return

    def _absorb_tool_calls(self, delta: Any) -> None:
        for tool_delta in getattr(delta, "tool_calls", None) or []:
            index = getattr(tool_delta, "index", 0) or 0
            slot = self.tool_calls.setdefault(int(index), _ToolCallParts())
            tool_id = getattr(tool_delta, "id", None)
            if tool_id and not slot.id:
                slot.id = str(tool_id)
            function = getattr(tool_delta, "function", None)
            if function is not None:
                name = getattr(function, "name", None)
                if name and not slot.name:
                    slot.name = str(name)
                arguments = getattr(function, "arguments", None)
                if isinstance(arguments, str) and arguments:
                    slot.arguments += arguments
            fields = getattr(tool_delta, "provider_specific_fields", None)
            if isinstance(fields, Mapping):
                if slot.provider_specific_fields is None:
                    slot.provider_specific_fields = {}
                for key, value in fields.items():
                    StreamAccumulator._merge_value_into(slot.provider_specific_fields, str(key), value)
            self.last_content_tokens += 1
            self.last_tool_call_tokens += 1
            self.saw_content_chunk = True

    def _absorb_provider_fields(self, delta: Any) -> None:
        thinking = getattr(delta, "thinking_blocks", None)
        if thinking:
            self.thinking_blocks.extend(thinking)
        reasoning_items = getattr(delta, "reasoning_items", None)
        if reasoning_items:
            self._merge_value_into(self.provider_specific_fields, "reasoning_items", list(reasoning_items))
        fields = getattr(delta, "provider_specific_fields", None)
        if isinstance(fields, Mapping):
            self._merge_provider_fields(fields)

    def _merge_provider_fields(self, incoming: Mapping[str, Any]) -> None:
        """Deep-merge provider_specific_fields emitted across streamed chunks."""
        for key, value in incoming.items():
            self._merge_value_into(self.provider_specific_fields, str(key), value)

    @staticmethod
    def _merge_value_into(target: dict[str, Any], key: str, value: Any) -> None:
        existing = target.get(key)
        if isinstance(existing, dict) and isinstance(value, Mapping):
            for sub_key, sub_value in value.items():
                StreamAccumulator._merge_value_into(existing, str(sub_key), sub_value)
        elif isinstance(existing, list) and isinstance(value, list):
            existing.extend(value)
        else:
            target[key] = value

    def partial_tool_call_issue(self) -> str | None:
        """Return diagnostic text when a streamed tool call is incomplete."""
        if not self.tool_calls:
            return None
        incomplete: list[dict[str, Any]] = []
        for index, parts in sorted(self.tool_calls.items()):
            reason: str | None = None
            if not parts.id:
                reason = "missing_id"
            elif not parts.name:
                reason = "missing_function_name"
            else:
                try:
                    json.loads(parts.arguments or "{}")
                except json.JSONDecodeError:
                    reason = "invalid_arguments_json"
            if reason is not None:
                incomplete.append({
                    "index": index,
                    "reason": reason,
                    "id": parts.id,
                    "name": parts.name,
                    "arguments_preview": (parts.arguments or "")[:300],
                })
        if not incomplete:
            return None
        return json.dumps(incomplete, sort_keys=True)


class StreamSession:
    """Single-use streaming session with watchdog and raw-header capture."""

    def __init__(self, raw_response: Any, *, watchdog: StreamWatchdog) -> None:
        self.raw = raw_response
        self.watchdog = watchdog
        self.accumulator = StreamAccumulator()
        headers = getattr(raw_response, "headers", {}) or {}
        self._raw_headers = {str(key): str(value) for key, value in dict(headers).items()}
        self._aipl_headers = AIPLResponseHeaders.from_response_headers(self._raw_headers)
        if self._aipl_headers.deployment_id:
            self.watchdog.deployment_id = self._aipl_headers.deployment_id
        self._started_at = time.monotonic()
        self._first_token_at: float | None = None
        self._ticker: asyncio.Task[None] | None = None

    async def __aenter__(self) -> StreamSession:
        """Anchor the watchdog clock and start the background ticker."""
        self.watchdog.start()
        if self.watchdog.enabled:
            self._ticker = asyncio.create_task(self._tick_loop())
        return self

    async def __aexit__(self, *_exc_info: object) -> None:
        """Stop the ticker and close the raw response if possible."""
        if self._ticker is not None:
            self._ticker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._ticker
        await _close_stream(self.raw)

    async def drain(self) -> StreamCompletion:
        """Drain the stream into a StreamCompletion."""
        stream = self.raw.parse()
        try:
            async for chunk in stream:
                self._raise_if_ticker_failed()
                self.accumulator.add_chunk(chunk)
                if self._first_token_at is None and self.accumulator.last_content_tokens > 0:
                    self._first_token_at = time.monotonic()
                if self.accumulator.last_content_tokens > 0:
                    tool_call_tokens = self.accumulator.last_tool_call_tokens
                    text_tokens = self.accumulator.last_content_tokens - tool_call_tokens
                    self.watchdog.on_content(text_tokens=text_tokens, tool_call_tokens=tool_call_tokens)
                self.watchdog.check()
        except StreamWatchdogError:
            raise
        except Exception as exc:
            if self.accumulator.saw_content_chunk:
                deployment_id = self._aipl_headers.deployment_id or "unknown"
                raise MidStreamProviderError(
                    f"Provider stream failed after partial output from deployment={deployment_id}: {exc}",
                    original=exc,
                    deployment_id=self._aipl_headers.deployment_id,
                    response_headers=dict(self._raw_headers),
                ) from exc
            raise
        finally:
            self._raise_if_ticker_failed()
        partial_tool_call = self.accumulator.partial_tool_call_issue()
        if partial_tool_call is not None:
            raise PartialToolCallStreamError(
                f"Provider stream ended with incomplete tool call(s): {partial_tool_call}",
                deployment_id=self._aipl_headers.deployment_id,
                response_headers=dict(self._raw_headers),
            )
        return StreamCompletion(
            response=self.accumulator.build_response(),
            usage=self.accumulator.usage,
            raw_headers=self._raw_headers,
            aipl_headers=self._aipl_headers,
            timing=TimingData(self._started_at, self._first_token_at, time.monotonic()),
            final_tps=self.watchdog.final_tps(),
        )

    async def _tick_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(self.watchdog.config.tick_seconds)
                self.watchdog.check()
        except BaseException:
            with contextlib.suppress(Exception):
                await _close_stream(self.raw)
            raise

    def _raise_if_ticker_failed(self) -> None:
        if self._ticker is None or not self._ticker.done() or self._ticker.cancelled():
            return
        exception = self._ticker.exception()
        if exception is not None:
            raise exception


async def _close_stream(raw: Any) -> None:
    closer = getattr(raw, "aclose", None)
    if callable(closer):
        result = closer()
        if isawaitable(result):
            await result
        return
    closer = getattr(raw, "close", None)
    if callable(closer):
        closer()


def _estimate_tokens(text: str) -> int:
    if not text:
        return 0
    return max(len(text) // 4, 1)
