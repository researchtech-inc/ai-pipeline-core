"""Class-based pipeline task runtime.

Rules enforced at class definition time for concrete tasks:
1. Subclasses must define ``@classmethod async def run(cls, ...)`` or inherit one from a parent task.
2. Every ``run()`` parameter after ``cls`` must use a supported annotation.
3. ``run()`` must return ``Document``, ``None``, ``list[Document]``, ``tuple[Document, ...]``, or unions of those shapes.
4. Bare ``Document`` is forbidden in both inputs and outputs; use concrete subclasses.
5. Class names must not start with ``Test``.
6. ``estimated_minutes`` must be >= 1, ``retries`` >= 0, and ``timeout_seconds`` positive when set.

Classes that explicitly declare ``_abstract_task = True`` skip definition-time validation.
Concrete subclasses of those abstract bases are validated normally.

Runtime behavior:
1. ``Task.run(...)`` returns an awaitable ``TaskHandle``.
2. ``await Task.run(...)`` executes the full lifecycle: retries, timeout, events, persistence, summaries, and replay capture.
3. Tasks must run inside an active pipeline execution context (or ``pipeline_test_context()`` in tests).
"""

import asyncio
import contextlib
import inspect
import logging
import math
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import timedelta
from functools import update_wrapper
from types import MappingProxyType
from typing import Any, ClassVar, cast
from uuid import UUID, uuid7

from prefect import task as _prefect_task
from prefect.cache_policies import NONE as _PREFECT_NO_CACHE
from prefect.context import FlowRunContext as _FlowRunContext

from ai_pipeline_core._base_exceptions import NonRetriableError, StubNotImplementedError
from ai_pipeline_core._lifecycle_events import DocumentRef, TaskCompletedEvent, TaskFailedEvent, TaskStartedEvent
from ai_pipeline_core._llm_core import CoreMessage, Role
from ai_pipeline_core._llm_core import generate as core_generate
from ai_pipeline_core.database import SpanKind, SpanRecord, SpanStatus
from ai_pipeline_core.database._documents import load_documents_from_database
from ai_pipeline_core.documents import Document
from ai_pipeline_core.pipeline._document_type_metadata import _FrozenDocumentTypesMeta, freeze_document_type_metadata
from ai_pipeline_core.pipeline._execution_context import (
    ExecutionContext,
    FlowFrame,
    TaskContext,
    TaskFrame,
    _TaskDocumentContext,
    assert_not_inside_task,
    get_execution_context,
    get_sinks,
    record_lifecycle_event,
    set_execution_context,
    set_task_context,
)
from ai_pipeline_core.pipeline._parallel import TaskHandle
from ai_pipeline_core.pipeline._span_types import SpanContext
from ai_pipeline_core.pipeline._task_cache import build_task_cache_key, build_task_description
from ai_pipeline_core.pipeline._task_runtime import (
    _attach_task_attempt,
    _class_name,
    _get_task_attempt,
    _input_documents,
    _maybe_with_timeout,
    _ordered_unique_document_types,
    _persist_documents_to_database,
    _TaskRunSpec,
)
from ai_pipeline_core.pipeline._track_span import track_span
from ai_pipeline_core.pipeline._type_validation import (
    resolve_type_hints,
    validate_task_argument_value,
    validate_task_input_annotation,
    validate_task_return_annotation,
)
from ai_pipeline_core.settings import settings

logger = logging.getLogger(__name__)

MILLISECONDS_PER_SECOND = 1000
MAX_RETRY_DELAY_SECONDS = 300
_RUN_SIGNATURE_WARNING_PARAMETER_COUNT = 8
_RUN_SIGNATURE_ERROR_PARAMETER_COUNT = 12

EVENT_PUBLISH_EXCEPTIONS = (OSError, RuntimeError, ValueError, TypeError)
SUMMARY_GENERATION_EXCEPTIONS = (Exception,)
TASK_EXECUTION_EXCEPTIONS = (Exception, asyncio.CancelledError)
RETRY_CAPTURE_EXCEPTIONS = (Exception,)
SUMMARY_EXCERPT_MAX_CHARS = 6000
_SUMMARY_PROMPT = "Write a concise 1-2 sentence summary of document '{name}'. Focus on the main topic and purpose.\n\nExcerpt:\n{excerpt}"

__all__ = ["PipelineTask"]


def _validate_run_parameter_count(task_name: str, parameters: list[inspect.Parameter]) -> None:
    """Warn on large task signatures and reject excessively large ones."""
    input_parameter_count = len(parameters) - 1
    if input_parameter_count >= _RUN_SIGNATURE_ERROR_PARAMETER_COUNT:
        raise TypeError(
            f"PipelineTask '{task_name}'.run declares {input_parameter_count} input parameters. "
            f"Task signatures may define at most {_RUN_SIGNATURE_ERROR_PARAMETER_COUNT - 1} inputs excluding 'cls'. "
            "Group related values into a frozen BaseModel parameter or a typed Document so the task interface stays explicit and maintainable."
        )
    if input_parameter_count >= _RUN_SIGNATURE_WARNING_PARAMETER_COUNT:
        logger.warning(
            "PipelineTask '%s'.run declares %d input parameters. Keep task signatures below %d inputs when possible. "
            "Group related values into a frozen BaseModel parameter or a typed Document to keep orchestration explicit.",
            task_name,
            input_parameter_count,
            _RUN_SIGNATURE_WARNING_PARAMETER_COUNT,
        )


class PipelineTask(metaclass=_FrozenDocumentTypesMeta):
    """Base class for pipeline tasks.

    Tasks are stateless units of work. Define ``run`` as a **@classmethod** because tasks
    carry no per-invocation instance state — all inputs arrive as arguments, all outputs
    are returned documents. The framework wraps ``run`` with retries, persistence,
    and event emission automatically.

    Set ``_abstract_task = True`` on an intermediate base class to skip ``run()``
    validation on that class. Concrete subclasses do not inherit that skip; they must
    define ``run()`` or inherit a validated implementation from a non-abstract parent.

    Minimal example::

        class SummarizeTask(PipelineTask):
            @classmethod
            async def run(cls, articles: tuple[ArticleDocument, ...]) -> tuple[SummaryDocument, ...]:
                conv = Conversation(model="gemini-3-flash").with_context(articles[0])
                conv = await conv.send("Summarize this article.")
                return (SummaryDocument.derive(derived_from=(articles[0],), name="summary.md", content=conv.content),)

    Calling ``await SummarizeTask.run((doc,))`` dispatches the full lifecycle. Calling without
    ``await`` returns a ``TaskHandle`` for parallel execution via ``collect_tasks``.
    """

    name: ClassVar[str]
    estimated_minutes: ClassVar[float] = 1.0
    retries: ClassVar[int | None] = None  # None = use Settings.task_retries
    retry_delay_seconds: ClassVar[int | None] = None  # None = use Settings.task_retry_delay_seconds
    timeout_seconds: ClassVar[int | None] = None
    cacheable: ClassVar[bool] = False
    cache_version: ClassVar[int] = 1
    cache_ttl_seconds: ClassVar[int | None] = None
    _abstract_task: ClassVar[bool] = False
    _stub: ClassVar[bool] = False
    BASE_COST_USD: ClassVar[float] = 0.0
    expected_cost: ClassVar[float | None] = None
    _document_types_frozen: ClassVar[bool] = False

    input_document_types: ClassVar[tuple[type[Document], ...]] = ()
    output_document_types: ClassVar[tuple[type[Document], ...]] = ()
    _run_spec: ClassVar[_TaskRunSpec]
    _prefect_task_fn: ClassVar[Any] = None

    def __init_subclass__(cls, *, stub: bool = False, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        cls._document_types_frozen = False
        cls._stub = stub
        if cls is PipelineTask:
            return
        if cls.__dict__.get("_abstract_task", False) is True:
            if stub:
                raise TypeError(
                    f"PipelineTask '{cls.__name__}' cannot use both stub=True and _abstract_task = True. "
                    "Use stub=True for placeholder classes awaiting implementation. "
                    "Use _abstract_task for intermediate base classes meant for override."
                )
            freeze_document_type_metadata(cls)
            return

        is_stub = stub

        from ai_pipeline_core.pipeline._file_rules import (  # noqa: PLC0415  # deferred: avoid import-order issues during package init
            is_exempt,
            register_stub,
            register_task,
            require_docstring,
        )

        exempt = is_exempt(cls)
        if not exempt:
            require_docstring(cls, kind="PipelineTask")

        cls._validate_class_config()

        own_run = cls.__dict__.get("run")
        if own_run is None:
            inherited_spec = getattr(cls, "_run_spec", None)
            if inherited_spec is None:
                raise TypeError(f"PipelineTask '{cls.__name__}' must define @classmethod async def run(cls, ...) or inherit a validated run() implementation.")
            cls.input_document_types = tuple(inherited_spec.input_document_types)
            cls.output_document_types = tuple(inherited_spec.output_document_types)
            cls._prefect_task_fn = cls._build_prefect_task()
        else:
            spec = cls._validate_run_signature(own_run)
            cls._run_spec = spec
            cls.input_document_types = tuple(spec.input_document_types)
            cls.output_document_types = tuple(spec.output_document_types)
            cls.run = classmethod(cls._build_run_wrapper(spec))
            cls._prefect_task_fn = cls._build_prefect_task()

        freeze_document_type_metadata(cls)

        # Register after all validation passes to avoid poisoning the registry on failure
        if not exempt:
            register_task(cls)
            if is_stub:
                register_stub(cls, kind="PipelineTask")

    @classmethod
    def _validate_class_config(cls) -> None:
        if cls.__name__.startswith("Test"):
            raise TypeError(
                f"PipelineTask class name cannot start with 'Test': {cls.__name__}. Use a production-style class name; pytest classes reserve the Test* prefix."
            )
        if "name" not in cls.__dict__:
            cls.name = cls.__name__
        if cls.estimated_minutes < 1:
            raise TypeError(f"PipelineTask '{cls.__name__}' has estimated_minutes={cls.estimated_minutes}. Use a value >= 1.")
        if not math.isfinite(cls.BASE_COST_USD) or cls.BASE_COST_USD < 0:
            raise TypeError(
                f"PipelineTask '{cls.__name__}' has BASE_COST_USD={cls.BASE_COST_USD}. "
                "Use a finite float >= 0 representing the minimum cost in USD per execution."
            )
        if cls.retries is not None and cls.retries < 0:
            raise TypeError(f"PipelineTask '{cls.__name__}' has retries={cls.retries}. Use a value >= 0.")
        if cls.retry_delay_seconds is not None and cls.retry_delay_seconds < 0:
            raise TypeError(f"PipelineTask '{cls.__name__}' has retry_delay_seconds={cls.retry_delay_seconds}. Use a value >= 0.")
        if cls.timeout_seconds is not None and cls.timeout_seconds <= 0:
            raise TypeError(f"PipelineTask '{cls.__name__}' has timeout_seconds={cls.timeout_seconds}. Use a positive integer timeout or None.")
        if cls.cache_version < 1:
            raise TypeError(f"PipelineTask '{cls.__name__}' has cache_version={cls.cache_version}. Use an integer >= 1.")
        if cls.cache_ttl_seconds is not None and cls.cache_ttl_seconds <= 0:
            raise TypeError(f"PipelineTask '{cls.__name__}' has cache_ttl_seconds={cls.cache_ttl_seconds}. Use a positive integer number of seconds or None.")

    @classmethod
    def _validate_run_signature(cls, run_descriptor: Any) -> _TaskRunSpec:
        if not isinstance(run_descriptor, classmethod):
            raise TypeError(f"PipelineTask '{cls.__name__}'.run must be declared with @classmethod.")

        descriptor = cast(object, run_descriptor)
        descriptor_func = getattr(descriptor, "__func__", None)
        if descriptor_func is None or not callable(descriptor_func):
            raise TypeError(f"PipelineTask '{cls.__name__}'.run descriptor is invalid. Declare it as @classmethod async def run(cls, ...).")

        user_run = cast(Callable[..., Awaitable[Any]], descriptor_func)
        if not inspect.iscoroutinefunction(user_run):
            raise TypeError(f"PipelineTask '{cls.__name__}'.run must be async def. Use async operations in task code and return Documents.")

        signature = inspect.signature(user_run)
        parameters = list(signature.parameters.values())
        if not parameters:
            raise TypeError(f"PipelineTask '{cls.__name__}'.run must accept 'cls' as the first parameter.")
        if parameters[0].name != "cls":
            raise TypeError(
                f"PipelineTask '{cls.__name__}'.run must use signature @classmethod async def run(cls, ...). Found first parameter '{parameters[0].name}'."
            )
        _validate_run_parameter_count(cls.__name__, parameters)

        for parameter in parameters[1:]:
            if parameter.kind == inspect.Parameter.VAR_POSITIONAL:
                raise TypeError(
                    f"PipelineTask '{cls.__name__}'.run parameter '*{parameter.name}' uses *args. "
                    f"Task inputs must be explicit named parameters. Replace *{parameter.name} with named parameters like 'documents: tuple[MyDocument, ...]'."
                )
            if parameter.kind == inspect.Parameter.VAR_KEYWORD:
                raise TypeError(
                    f"PipelineTask '{cls.__name__}'.run parameter '**{parameter.name}' uses **kwargs. "
                    f"Task inputs must be explicit named parameters. Replace **{parameter.name} with named parameters like 'config: MyConfig'."
                )

        hints = resolve_type_hints(user_run)
        input_document_types: list[type[Document]] = []
        for parameter in parameters[1:]:
            annotation = hints.get(parameter.name)
            if annotation is None:
                raise TypeError(
                    f"PipelineTask '{cls.__name__}'.run parameter '{parameter.name}' is missing a type annotation. Annotate every task input explicitly."
                )
            input_document_types.extend(
                validate_task_input_annotation(
                    annotation,
                    task_name=cls.__name__,
                    parameter_name=parameter.name,
                )
            )

        return_annotation = hints.get("return")
        if return_annotation is None:
            raise TypeError(
                f"PipelineTask '{cls.__name__}'.run is missing a return annotation. "
                "Return Document, None, list[Document], tuple[Document, ...], or unions of those shapes."
            )
        output_document_types = validate_task_return_annotation(return_annotation, task_name=cls.__name__)

        return _TaskRunSpec(
            user_run=user_run,
            signature=signature,
            hints=MappingProxyType(hints),
            input_document_types=_ordered_unique_document_types(input_document_types),
            output_document_types=_ordered_unique_document_types(output_document_types),
        )

    @classmethod
    def _public_signature(cls) -> inspect.Signature:
        parameters = tuple(cls._run_spec.signature.parameters.values())[1:]
        return cls._run_spec.signature.replace(parameters=parameters)

    @classmethod
    def _bind_run_arguments(cls, args: tuple[Any, ...], kwargs: dict[str, Any]) -> dict[str, Any]:
        try:
            bound = cls._run_spec.signature.bind(cls, *args, **kwargs)
        except TypeError as exc:
            raise TypeError(f"PipelineTask '{cls.__name__}.run' called with invalid arguments. Expected signature {cls._public_signature()}: {exc}") from exc

        bound.apply_defaults()
        arguments = {name: value for name, value in bound.arguments.items() if name != "cls"}
        for name, value in arguments.items():
            validate_task_argument_value(
                task_name=cls.__name__,
                parameter_name=name,
                value=value,
                annotation=cls._run_spec.hints[name],
            )
        return arguments

    @classmethod
    def _build_run_wrapper(cls, spec: _TaskRunSpec) -> Callable[..., TaskHandle[tuple[Document[Any], ...]]]:
        def wrapped(task_cls: type[PipelineTask], *args: Any, **kwargs: Any) -> TaskHandle[tuple[Document[Any], ...]]:
            if task_cls._stub:
                raise StubNotImplementedError(
                    f"PipelineTask '{task_cls.__name__}' is a stub (_stub = True) and cannot be executed. "
                    f"Implement the run() body and remove 'stub=True' from the class declaration to enable execution."
                )
            arguments = task_cls._bind_run_arguments(args, kwargs)
            assert_not_inside_task(task_cls.__name__)
            try:
                asyncio.get_running_loop()
            except RuntimeError as exc:
                raise RuntimeError(
                    f"PipelineTask '{task_cls.__name__}.run' must be called from async code. Use `await Task.run(...)` inside a flow or test context."
                ) from exc

            execution_ctx = get_execution_context()
            if execution_ctx is None:
                raise RuntimeError(
                    f"PipelineTask '{task_cls.__name__}.run' called outside pipeline execution context. "
                    "Run tasks inside PipelineFlow/PipelineDeployment execution or pipeline_test_context()."
                )

            task = asyncio.create_task(task_cls._execute_invocation(arguments))
            handle = TaskHandle(
                _task=task,
                task_class=task_cls,
                input_arguments=MappingProxyType(dict(arguments)),
            )
            execution_ctx.active_task_handles.add(handle)
            task.add_done_callback(lambda _finished: execution_ctx.active_task_handles.discard(handle))
            return handle

        wrapped.__name__ = spec.user_run.__name__
        wrapped.__qualname__ = spec.user_run.__qualname__
        wrapped.__doc__ = spec.user_run.__doc__
        wrapped.__signature__ = cls._public_signature()  # type: ignore[attr-defined]
        return update_wrapper(wrapped, spec.user_run)

    @classmethod
    def _build_prefect_task(cls) -> Any:
        """Build a Prefect Task object wrapping the user's run() implementation.

        Created at class definition time. Prefect 3.x Task.__init__ is pure Python
        with no server registration — safe at import time.
        Retries are always owned by the framework (not Prefect). Setting retries=0
        on the Prefect decorator disables Prefect's retry engine entirely.
        Caching is disabled (handled by the framework's ClickHouse cache).
        """
        task_cls = cls

        async def _prefect_body(arguments: dict[str, Any]) -> tuple[Document[Any], ...]:
            return await task_cls._run_and_normalize(arguments)

        return _prefect_task(
            name=cls.name,
            task_run_name=cls.name,
            retries=0,
            retry_delay_seconds=0,
            timeout_seconds=cls.timeout_seconds,
            persist_result=False,
            cache_policy=_PREFECT_NO_CACHE,
            log_prints=False,
        )(_prefect_body)

    @classmethod
    async def _persist_documents(
        cls,
        documents: tuple[Document, ...],
    ) -> tuple[Document, ...]:
        """Deduplicate and persist documents to the database."""
        if not documents:
            return ()

        deduped = _TaskDocumentContext.deduplicate(list(documents))
        execution_ctx = get_execution_context()
        if execution_ctx is not None:
            await _persist_documents_to_database(deduped, execution_ctx.database)

        return documents

    @classmethod
    async def _run_with_retries(
        cls,
        arguments: Mapping[str, Any],
        *,
        parent_span_ctx: SpanContext,
    ) -> tuple[tuple[Document, ...], int]:
        """Execute the task body, always wrapped in an ATTEMPT span.

        Every executed body — even with retries=0 — emits one ATTEMPT span so the span tree
        is TASK → ATTEMPT → children. Exception: cache hits emit only a TASK span with
        CACHED status and no ATTEMPT. When retries=0 and the task fails, the ATTEMPT span
        captures error_json but the parent TASK span does NOT accumulate retry_errors
        (retry metadata is only meaningful when retries > 0).
        """
        retries = cls.retries if cls.retries is not None else settings.task_retries
        retry_delay = cls.retry_delay_seconds if cls.retry_delay_seconds is not None else settings.task_retry_delay_seconds
        attempts = retries + 1
        use_prefect = _FlowRunContext.get() is not None and cls._prefect_task_fn is not None

        for attempt in range(attempts):
            attempt_span_id = uuid7()
            try:
                async with track_span(
                    SpanKind.ATTEMPT,
                    f"attempt-{attempt}",
                    "",
                    sinks=get_sinks(),
                    span_id=attempt_span_id,
                ) as attempt_ctx:
                    attempt_ctx.set_meta(attempt=attempt, max_attempts=attempts)
                    if use_prefect:
                        result = await cls._prefect_task_fn(dict(arguments))
                    else:
                        result = await _maybe_with_timeout(cls.timeout_seconds, lambda: cls._run_and_normalize(arguments))
                    return result, attempt
            except (NonRetriableError, asyncio.CancelledError) as exc:
                _attach_task_attempt(exc, attempt)
                raise
            except RETRY_CAPTURE_EXCEPTIONS as exc:
                will_retry = attempt < attempts - 1
                delay = min(retry_delay * (2**attempt), MAX_RETRY_DELAY_SECONDS) if will_retry else 0
                if attempts > 1:
                    parent_span_ctx.record_retry_failure(
                        exc=exc,
                        attempt=attempt,
                        max_attempts=attempts,
                        attempt_span_id=str(attempt_span_id),
                        will_retry=will_retry,
                        delay_seconds=delay,
                    )
                if not will_retry:
                    _attach_task_attempt(exc, attempt)
                    raise
                logger.warning(
                    "Task %s attempt %d/%d failed, retrying in %ds",
                    cls.name,
                    attempt + 1,
                    attempts,
                    delay,
                    exc_info=exc,
                )
                record_lifecycle_event(
                    "task.retry",
                    f"Task {cls.name} attempt {attempt + 1}/{attempts} failed, retrying in {delay}s",
                    task_name=cls.name,
                    task_class=cls.__name__,
                    attempt=attempt + 1,
                    max_attempts=attempts,
                    delay_seconds=delay,
                    error_type=type(exc).__name__,
                    error_message=str(exc),
                )
                await asyncio.sleep(delay)

        raise RuntimeError(f"PipelineTask '{cls.__name__}' exhausted {attempts} retry attempts without raising.")

    @classmethod
    async def _run_and_normalize(cls, arguments: Mapping[str, Any]) -> tuple[Document, ...]:
        result = await cls._run_spec.user_run(cls, **dict(arguments))
        result_docs = cls._normalize_result_documents(result)
        if result_docs:
            input_docs = _input_documents(arguments)
            if input_docs:
                input_sha256s = frozenset(document.sha256 for document in input_docs)
                passthrough_docs = [document for document in result_docs if document.sha256 in input_sha256s]
                if passthrough_docs:
                    passthrough_names = ", ".join(f"'{document.name}'" for document in passthrough_docs)
                    logger.warning(
                        "PipelineTask '%s' returned input document(s) unchanged: %s. "
                        "Tasks should create new documents via derive(), create(), create_external(), or create_root(). "
                        "The deployment blackboard carries earlier artifacts automatically, so tasks should not forward input documents unchanged.",
                        cls.__name__,
                        passthrough_names,
                    )
        return result_docs

    @classmethod
    def _task_cache_ttl(cls, execution_ctx: ExecutionContext) -> timedelta | None:
        if execution_ctx.disable_cache:
            return None
        if cls.cache_ttl_seconds is not None:
            return timedelta(seconds=cls.cache_ttl_seconds)
        return execution_ctx.cache_ttl

    @classmethod
    async def _load_cached_outputs(
        cls,
        database: Any,
        output_document_shas: tuple[str, ...],
    ) -> tuple[Document, ...] | None:
        if not output_document_shas:
            logger.warning(
                "Task cache span for '%s' has no output_document_shas. Persist cached task outputs on the completed span before expecting cache reuse.",
                cls.__name__,
            )
            return None
        documents = await load_documents_from_database(
            database,
            set(output_document_shas),
            filter_types=list(cls.output_document_types) if cls.output_document_types else None,
        )
        documents_by_sha = {str(document.sha256): document for document in documents}
        missing_shas = [sha for sha in output_document_shas if sha not in documents_by_sha]
        if missing_shas:
            logger.warning(
                "Task cache hit for '%s' could not hydrate cached documents %s. "
                "Persist every cached output document and blob before reusing task cache entries.",
                cls.__name__,
                ", ".join(missing_shas),
            )
            return None
        return tuple(documents_by_sha[sha] for sha in output_document_shas)

    @classmethod
    async def _reuse_cached_output(
        cls,
        *,
        arguments: Mapping[str, Any],
        execution_ctx: ExecutionContext,
        task_frame: TaskFrame,
        task_span_id: UUID,
        parent_span_id: UUID | None,
        task_description: str,
        input_docs: tuple[Document, ...],
        task_cache_key: str,
        task_cache_source_span: SpanRecord,
        database: Any,
    ) -> tuple[Document, ...] | None:
        cached_documents = await cls._load_cached_outputs(database, task_cache_source_span.output_document_shas)
        if cached_documents is None:
            return None

        start_time = time.monotonic()
        output_refs = cls._build_output_refs(cached_documents)
        flow_step = execution_ctx.flow_frame.step if execution_ctx.flow_frame is not None else 0
        cached_input_sha256s = tuple(doc.sha256 for doc in input_docs)
        await cls._emit_task_completed(
            execution_ctx,
            execution_ctx.flow_frame,
            step=flow_step,
            task_name=cls.name,
            task_class_name=cls.__name__,
            span_id=str(task_span_id),
            start_time=start_time,
            output_documents=output_refs,
            status=str(SpanStatus.CACHED),
            input_document_sha256s=cached_input_sha256s,
        )
        record_lifecycle_event(
            "task.cached",
            f"Reused cached task output for {cls.name}",
            task_name=cls.name,
            task_class=cls.__name__,
            flow_name=execution_ctx.flow_frame.name if execution_ctx.flow_frame is not None else "",
            step=flow_step,
            cache_key=task_cache_key,
            cache_source_span_id=str(task_cache_source_span.span_id),
        )
        with contextlib.ExitStack() as cached_stack:
            cached_stack.enter_context(set_execution_context(execution_ctx.with_task(task_frame)))
            async with track_span(
                SpanKind.TASK,
                cls.name,
                f"classmethod:{cls.__module__}:{cls.__qualname__}.run",
                sinks=get_sinks(),
                span_id=task_span_id,
                parent_span_id=parent_span_id,
                encode_receiver={"mode": "constructor_args", "value": {"task_class": cls}},
                encode_input=dict(arguments),
                db=database,
                input_preview={
                    "task_class": cls.__name__,
                    "input_documents": [doc.name for doc in input_docs],
                },
            ) as span_ctx:
                span_ctx.set_status(SpanStatus.CACHED)
                span_ctx.set_meta(
                    attempt=0,
                    retries=cls.retries,
                    retry_delay_seconds=cls.retry_delay_seconds,
                    timeout_seconds=cls.timeout_seconds,
                    cache_hit=True,
                    cache_key=task_cache_key,
                    cache_source_span_id=str(task_cache_source_span.span_id),
                    description=task_description,
                )
                span_ctx.set_output_preview({"documents": [doc.name for doc in cached_documents]})
                span_ctx._set_output_value(cached_documents)
        return cached_documents

    @classmethod
    def _normalize_result_documents(cls, result: Any) -> tuple[Document[Any], ...]:
        if result is None:
            return ()
        if isinstance(result, Document):
            raw_items = cast(Sequence[Any], (result,))
        elif isinstance(result, (list, tuple)):
            raw_items = cast(Sequence[Any], result)
        else:
            raise TypeError(
                f"PipelineTask '{cls.__name__}' returned {type(result).__name__}. "
                "run() must return Document, None, list[Document], tuple[Document, ...], or unions of those shapes."
            )

        normalized_docs: list[Document[Any]] = []
        bad_types: set[str] = set()
        for item in raw_items:
            if isinstance(item, Document):
                normalized_docs.append(cast(Document[Any], item))
                continue
            bad_types.add(_class_name(type(item)))

        if bad_types:
            bad_types_text = ", ".join(sorted(bad_types))
            raise TypeError(f"PipelineTask '{cls.__name__}' returned non-Document items ({bad_types_text}). run() must return only Document subclasses.")
        return tuple(normalized_docs)

    @staticmethod
    async def _emit_task_started(
        execution_ctx: ExecutionContext,
        flow_frame: FlowFrame | None,
        *,
        step: int,
        task_name: str,
        task_class_name: str,
        span_id: str,
        input_document_sha256s: tuple[str, ...] = (),
    ) -> None:
        if flow_frame is None:
            return
        root_id = execution_ctx.root_deployment_id or execution_ctx.deployment_id or execution_ctx.span_id
        if root_id is None:
            raise RuntimeError("Task started event cannot be published without a root deployment id.")
        try:
            await execution_ctx.publisher.publish_task_started(
                TaskStartedEvent(
                    run_id=execution_ctx.run_id,
                    span_id=span_id,
                    root_deployment_id=str(root_id),
                    parent_deployment_task_id=str(execution_ctx.parent_deployment_task_id) if execution_ctx.parent_deployment_task_id else None,
                    flow_name=flow_frame.name,
                    step=step,
                    total_steps=flow_frame.total_steps,
                    status=str(SpanStatus.RUNNING),
                    task_name=task_name,
                    task_class=task_class_name,
                    parent_span_id=str(execution_ctx.flow_span_id or ""),
                    input_document_sha256s=input_document_sha256s,
                )
            )
        except EVENT_PUBLISH_EXCEPTIONS as exc:
            logger.warning("Task started event publish failed for '%s': %s", task_name, exc)

    @staticmethod
    async def _emit_task_completed(
        execution_ctx: ExecutionContext,
        flow_frame: FlowFrame | None,
        *,
        step: int,
        task_name: str,
        task_class_name: str,
        span_id: str,
        start_time: float,
        output_documents: tuple[DocumentRef, ...],
        status: str = str(SpanStatus.COMPLETED),
        input_document_sha256s: tuple[str, ...] = (),
    ) -> None:
        if flow_frame is None:
            return
        root_id = execution_ctx.root_deployment_id or execution_ctx.deployment_id or execution_ctx.span_id
        if root_id is None:
            raise RuntimeError("Task completed event cannot be published without a root deployment id.")
        try:
            await execution_ctx.publisher.publish_task_completed(
                TaskCompletedEvent(
                    run_id=execution_ctx.run_id,
                    span_id=span_id,
                    root_deployment_id=str(root_id),
                    parent_deployment_task_id=str(execution_ctx.parent_deployment_task_id) if execution_ctx.parent_deployment_task_id else None,
                    flow_name=flow_frame.name,
                    step=step,
                    total_steps=flow_frame.total_steps,
                    status=status,
                    task_name=task_name,
                    task_class=task_class_name,
                    duration_ms=int((time.monotonic() - start_time) * MILLISECONDS_PER_SECOND),
                    output_documents=output_documents,
                    parent_span_id=str(execution_ctx.flow_span_id or ""),
                    input_document_sha256s=input_document_sha256s,
                )
            )
        except EVENT_PUBLISH_EXCEPTIONS as exc:
            logger.warning("Task completed event publish failed for '%s': %s", task_name, exc)

    @staticmethod
    async def _emit_task_failed(
        execution_ctx: ExecutionContext,
        flow_frame: FlowFrame | None,
        *,
        step: int,
        task_name: str,
        task_class_name: str,
        span_id: str,
        error_message: str,
        input_document_sha256s: tuple[str, ...] = (),
    ) -> None:
        if flow_frame is None:
            return
        root_id = execution_ctx.root_deployment_id or execution_ctx.deployment_id or execution_ctx.span_id
        if root_id is None:
            raise RuntimeError("Task failed event cannot be published without a root deployment id.")
        try:
            await execution_ctx.publisher.publish_task_failed(
                TaskFailedEvent(
                    run_id=execution_ctx.run_id,
                    span_id=span_id,
                    root_deployment_id=str(root_id),
                    parent_deployment_task_id=str(execution_ctx.parent_deployment_task_id) if execution_ctx.parent_deployment_task_id else None,
                    flow_name=flow_frame.name,
                    step=step,
                    total_steps=flow_frame.total_steps,
                    status=str(SpanStatus.FAILED),
                    task_name=task_name,
                    task_class=task_class_name,
                    error_message=error_message,
                    parent_span_id=str(execution_ctx.flow_span_id or ""),
                    input_document_sha256s=input_document_sha256s,
                )
            )
        except EVENT_PUBLISH_EXCEPTIONS as exc:
            logger.warning("Task failed event publish failed for '%s': %s", task_name, exc)

    @staticmethod
    async def _generate_summaries(documents: Sequence[Document]) -> None:
        """Generate missing summaries via LLM and set them directly on documents."""
        for document in documents:
            if document.summary:
                continue
            if not settings.doc_summary_enabled or not document.is_text:
                continue
            try:
                excerpt = document.text[:SUMMARY_EXCERPT_MAX_CHARS]
                response = await core_generate(
                    [CoreMessage(role=Role.USER, content=_SUMMARY_PROMPT.format(name=document.name, excerpt=excerpt))],
                    model=settings.doc_summary_model,
                    purpose="doc_summary",
                )
                generated = response.content.strip()
                if generated:
                    object.__setattr__(document, "summary", generated)
            except SUMMARY_GENERATION_EXCEPTIONS as exc:
                logger.warning("Inline summary generation failed for '%s': %s", document.name, exc)

    @staticmethod
    def _build_output_refs(documents: Sequence[Document]) -> tuple[DocumentRef, ...]:
        return tuple(
            DocumentRef(
                sha256=document.sha256,
                class_name=type(document).__name__,
                name=document.name,
                summary=document.summary,
                publicly_visible=getattr(type(document), "publicly_visible", False),
                derived_from=tuple(document.derived_from),
                triggered_by=tuple(document.triggered_by),
            )
            for document in documents
        )

    @classmethod
    async def _execute_lifecycle(
        cls,
        arguments: Mapping[str, Any],
        *,
        execution_ctx: ExecutionContext,
        flow_frame: FlowFrame | None,
        task_name: str,
        span_id: str,
        flow_step: int,
        start_time: float,
        input_document_sha256s: tuple[str, ...] = (),
        parent_span_ctx: SpanContext,
    ) -> tuple[tuple[Document, ...], int]:
        """Execute task lifecycle with events and persistence."""
        await cls._emit_task_started(
            execution_ctx,
            flow_frame,
            step=flow_step,
            task_name=task_name,
            task_class_name=cls.__name__,
            span_id=span_id,
            input_document_sha256s=input_document_sha256s,
        )
        record_lifecycle_event(
            "task.started",
            f"Starting task {task_name}",
            task_name=task_name,
            task_class=cls.__name__,
            flow_name=flow_frame.name if flow_frame is not None else "",
            step=flow_step,
        )
        try:
            documents, attempt = await cls._run_with_retries(arguments, parent_span_ctx=parent_span_ctx)

            await cls._generate_summaries(documents)
            persisted_docs = await cls._persist_documents(documents)

            await cls._emit_task_completed(
                execution_ctx,
                flow_frame,
                step=flow_step,
                task_name=task_name,
                task_class_name=cls.__name__,
                span_id=span_id,
                start_time=start_time,
                output_documents=cls._build_output_refs(persisted_docs),
                input_document_sha256s=input_document_sha256s,
            )
            record_lifecycle_event(
                "task.completed",
                f"Completed task {task_name}",
                task_name=task_name,
                task_class=cls.__name__,
                flow_name=flow_frame.name if flow_frame is not None else "",
                step=flow_step,
                output_count=len(persisted_docs),
            )
            return persisted_docs, attempt
        except TASK_EXECUTION_EXCEPTIONS as exc:
            await cls._emit_task_failed(
                execution_ctx,
                flow_frame,
                step=flow_step,
                task_name=task_name,
                task_class_name=cls.__name__,
                span_id=span_id,
                error_message=str(exc),
                input_document_sha256s=input_document_sha256s,
            )
            record_lifecycle_event(
                "task.failed",
                f"Task {task_name} failed",
                task_name=task_name,
                task_class=cls.__name__,
                flow_name=flow_frame.name if flow_frame is not None else "",
                step=flow_step,
                error_type=type(exc).__name__,
                error_message=str(exc),
            )
            raise

    @classmethod
    async def _execute_invocation(cls, arguments: Mapping[str, Any]) -> tuple[Document, ...]:
        """Execute task lifecycle with events, summaries, and persistence."""
        execution_ctx = get_execution_context()
        if execution_ctx is None:
            raise RuntimeError(
                f"PipelineTask '{cls.__name__}.run' called outside pipeline execution context. "
                "Run tasks inside PipelineFlow/PipelineDeployment execution or pipeline_test_context()."
            )

        parent_task = execution_ctx.task_frame
        task_span_id = uuid7()
        task_function_path = f"{cls.__module__}:{cls.__qualname__}"
        task_frame = TaskFrame(
            task_class_name=cls.__name__,
            task_id=str(task_span_id),
            depth=(parent_task.depth + 1) if parent_task else 0,
            parent=parent_task,
        )

        database = execution_ctx.database
        input_docs = _input_documents(arguments)
        parent_span_id = execution_ctx.current_span_id or execution_ctx.span_id
        task_description = build_task_description(arguments)
        task_cache_key = ""
        task_cache_source_span: SpanRecord | None = None
        if cls.cacheable:
            task_cache_ttl = cls._task_cache_ttl(execution_ctx)
            if database is not None and task_cache_ttl is not None:
                task_cache_key = build_task_cache_key(
                    task_class_path=task_function_path,
                    cache_version=cls.cache_version,
                    arguments=arguments,
                )
                task_cache_source_span = await cast(Any, database).get_cached_completion(task_cache_key, max_age=task_cache_ttl)
                if task_cache_source_span is not None:
                    cached_documents = await cls._reuse_cached_output(
                        arguments=arguments,
                        execution_ctx=execution_ctx,
                        task_frame=task_frame,
                        task_span_id=task_span_id,
                        parent_span_id=parent_span_id,
                        task_description=task_description,
                        input_docs=tuple(input_docs),
                        task_cache_key=task_cache_key,
                        task_cache_source_span=task_cache_source_span,
                        database=database,
                    )
                    if cached_documents is not None:
                        return cached_documents

        task_exec_ctx = execution_ctx.with_task(task_frame)
        task_ctx = TaskContext(task_class_name=cls.__name__)

        start_time = time.monotonic()
        task_name = cls.name
        flow_frame = execution_ctx.flow_frame
        input_doc_names = [doc.name for doc in input_docs]
        task_target = f"classmethod:{cls.__module__}:{cls.__qualname__}.run"
        task_receiver = {"mode": "constructor_args", "value": {"task_class": cls}}
        task_input_preview = {
            "task_class": cls.__name__,
            "input_documents": input_doc_names,
        }

        with contextlib.ExitStack() as stack:
            stack.enter_context(set_execution_context(task_exec_ctx))
            stack.enter_context(set_task_context(task_ctx))
            task_attempt = 0

            async with track_span(
                SpanKind.TASK,
                task_name,
                task_target,
                sinks=get_sinks(),
                span_id=task_span_id,
                parent_span_id=parent_span_id,
                encode_receiver=task_receiver,
                encode_input=dict(arguments),
                db=database,
                input_preview=task_input_preview,
            ) as span_ctx:
                if cls.BASE_COST_USD > 0:
                    span_ctx._add_cost(cls.BASE_COST_USD)
                span_ctx.set_meta(
                    attempt=task_attempt,
                    retries=cls.retries,
                    retry_delay_seconds=cls.retry_delay_seconds,
                    timeout_seconds=cls.timeout_seconds,
                    cache_hit=False,
                    description=task_description,
                    cache_key=task_cache_key,
                )
                try:
                    input_sha256s = tuple(doc.sha256 for doc in input_docs)
                    result, task_attempt = await cls._execute_lifecycle(
                        arguments,
                        execution_ctx=execution_ctx,
                        flow_frame=flow_frame,
                        task_name=task_name,
                        span_id=str(span_ctx.span_id),
                        flow_step=flow_frame.step if flow_frame is not None else 0,
                        start_time=start_time,
                        input_document_sha256s=input_sha256s,
                        parent_span_ctx=span_ctx,
                    )
                except TASK_EXECUTION_EXCEPTIONS as exc:
                    task_attempt = _get_task_attempt(exc)
                    span_ctx.set_meta(
                        attempt=task_attempt,
                        retries=cls.retries,
                        retry_delay_seconds=cls.retry_delay_seconds,
                        timeout_seconds=cls.timeout_seconds,
                        cache_hit=False,
                        description=task_description,
                        cache_key=task_cache_key,
                    )
                    raise

                span_ctx.set_meta(
                    attempt=task_attempt,
                    retries=cls.retries,
                    retry_delay_seconds=cls.retry_delay_seconds,
                    timeout_seconds=cls.timeout_seconds,
                    cache_hit=False,
                    description=task_description,
                    cache_key=task_cache_key,
                )
                span_ctx.set_output_preview({"documents": [doc.name for doc in result]})
                span_ctx._set_output_value(result)
                return result


freeze_document_type_metadata(PipelineTask)
