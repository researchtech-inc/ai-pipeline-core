# MODULE: pipeline
# CLASSES: LimitKind, PipelineLimit, FlowOptions, PipelineFlow, TaskHandle, TaskBatch, PipelineTask
# DEPENDS: BaseModel, StrEnum
# PURPOSE: Pipeline framework primitives.
# VERSION: 0.22.0
# AUTO-GENERATED from source code — do not edit. Run: make docs-ai-build

## Imports

```python
from ai_pipeline_core import (
    FlowOptions,
    LimitKind,
    PipelineFlow,
    PipelineLimit,
    PipelineTask,
    TaskBatch,
    TaskHandle,
    add_cost,
    as_task_completed,
    collect_tasks,
    get_run_id,
    pipeline_concurrency,
    pipeline_test_context,
    safe_gather,
    safe_gather_indexed,
    traced_operation,
)
```

## Public API

```python
# Enum
class LimitKind(StrEnum):
    """Kind of concurrency/rate limit.

    CONCURRENT: Slots held for duration of operation (lease-based).
        limit=500 means at most 500 simultaneous operations across all runs.

    PER_MINUTE: Token bucket with limit/60 decay per second.
        Allows bursting up to `limit` immediately, then refills gradually.
        NOT a sliding window.

    PER_HOUR: Token bucket with limit/3600 decay per second. Same burst semantics."""

    CONCURRENT = "concurrent"
    PER_MINUTE = "per_minute"
    PER_HOUR = "per_hour"


@dataclass(frozen=True, slots=True)
class PipelineLimit:
    """Concurrency/rate limit configuration.

    Must use names matching ``[a-zA-Z0-9_-]+`` in PipelineDeployment.concurrency_limits (validated at class definition time)."""

    limit: int
    kind: LimitKind = LimitKind.CONCURRENT
    timeout: int = 600

    def __post_init__(self) -> None:
        if self.limit < 1:
            raise ValueError(f"limit must be >= 1, got {self.limit}")
        if self.timeout <= 0:
            raise ValueError(f"timeout must be > 0, got {self.timeout}")


class FlowOptions(BaseModel):
    """Base configuration for pipeline flows.

    Use FlowOptions for deployment/environment configuration that may
    differ between environments (dev/staging/production).

    Never inherit from FlowOptions for task-level options, writer configs,
    or programmatically-constructed parameter objects — use BaseModel instead."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class PipelineFlow:
    """Base class for pipeline flows.

    Flows are the unit of resume, progress tracking, and document hand-off in a deployment.
    Define ``run`` as an **instance method** (not ``@classmethod``) because flows can carry
    per-instance configuration passed via ``build_flows()``::

        class EvidenceFlow(PipelineFlow):
            round_number: int

            async def run(
                self,
                planning: PlanningContextDocument,
                state: EvidenceStateDocument | None,
                options: ResearchOptions,
            ) -> tuple[EvidenceDocument, ...]:
                ...

    Resolution rules:
    - ``planning: PlanningContextDocument`` -> latest document of that type
    - ``state: EvidenceStateDocument | None`` -> latest, or ``None`` if absent
    - ``dossiers: tuple[DossierDocument, ...]`` -> all accumulated documents of that type
    - ``options: ResearchOptions`` -> the deployment options object

    The deployment creates flow instances with constructor kwargs::

        def build_flows(self, options):
            return [EvidenceFlow(round_number=1), EvidenceFlow(round_number=2)]

    Each instance runs independently with its own parameters, resume record, and progress.
    Constructor kwargs are captured for replay serialization via ``get_params()``.
    Use ``get_run_id()`` from ``ai_pipeline_core.pipeline`` to access the run ID inside a flow."""

    name: ClassVar[str]
    estimated_minutes: ClassVar[float] = 1.0
    retries: ClassVar[int | None] = None
    retry_delay_seconds: ClassVar[int | None] = None  # None = use Settings.flow_retry_delay_seconds
    BASE_COST_USD: ClassVar[float] = 0.0
    max_fan_out: ClassVar[int | None] = None  # Maximum expected fan-out for this flow.
    input_document_types: ClassVar[tuple[type[Document], ...]] = ()
    output_document_types: ClassVar[tuple[type[Document], ...]] = ()
    task_graph: ClassVar[list[tuple[str, str, float]]] = []

    def __init__(self, **kwargs: Any) -> None:
        """Constructor for per-flow instance configuration."""
        cls = type(self)
        known_params: set[str] = set()
        for klass in cls.__mro__:
            known_params.update(_declared_init_annotations(klass))
            known_params.update(
                name
                for name, value in vars(klass).items()
                if not name.startswith("_") and not callable(value) and not isinstance(value, (classmethod, staticmethod, property))
            )
        unknown = sorted(key for key in kwargs if key not in known_params)
        if unknown:
            allowed = ", ".join(sorted(known_params)) or "(none)"
            raise TypeError(f"PipelineFlow '{cls.__name__}' got unknown init parameter(s): {', '.join(unknown)}. Allowed parameters: {allowed}.")
        self._params: dict[str, Any] = dict(kwargs)
        for key, value in kwargs.items():
            setattr(self, key, value)

    @classmethod
    def expected_tasks(cls) -> list[dict[str, Any]]:
        """Return expected task metadata extracted from run() AST."""
        return [{"name": name, "estimated_minutes": minutes} for name, _mode, minutes in cls.task_graph]

    def __init_subclass__(cls, *, stub: bool = False, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        cls._document_types_frozen = False
        cls._stub = stub
        if cls is PipelineFlow:
            return
        if cls.__dict__.get("_abstract_flow", False) is True:
            if stub:
                raise TypeError(
                    f"PipelineFlow '{cls.__name__}' cannot use both stub=True and _abstract_flow = True. "
                    "Use stub=True for placeholder classes awaiting implementation. "
                    "Use _abstract_flow for intermediate base classes meant for override."
                )
            freeze_document_type_metadata(cls)
            return

        is_stub = stub

        from ai_pipeline_core.pipeline._file_rules import (  # noqa: PLC0415  # deferred: avoid import-order issues during package init
            is_exempt,
            register_flow,
            register_stub,
            require_docstring,
        )

        exempt = is_exempt(cls)
        if not exempt:
            require_docstring(cls, kind="PipelineFlow")

        cls._validate_class_config()
        run_fn, hints, params = cls._validate_run_signature()
        input_types, output_types = cls._extract_document_types(hints, params)
        cls.input_document_types = tuple(input_types)
        cls.output_document_types = tuple(output_types)
        cls.task_graph = cls._parse_task_graph(run_fn)
        _warn_on_unused_task_outputs(cls.__name__, _parse_task_bindings_from_source(run_fn))
        cls._prefect_flow_fn = cls._build_prefect_flow()
        freeze_document_type_metadata(cls)

        # Register after all validation passes to avoid poisoning the registry on failure
        if not exempt:
            register_flow(cls)
            if is_stub:
                register_stub(cls, kind="PipelineFlow")

    def get_params(self) -> dict[str, Any]:
        """Return constructor params for flow plan serialization."""
        return dict(getattr(self, "_params", {}))

    async def run(self, **kwargs: Any) -> tuple[Any, ...]:
        """Execute the flow. Subclasses provide concrete typed annotations."""
        raise NotImplementedError


@dataclass(frozen=True, slots=True, eq=False)
class TaskHandle:
    """Handle for an executing pipeline task."""

    task_class: type[Any] | None
    input_arguments: Mapping[str, Any]

    @property
    def done(self) -> bool:
        """Whether the underlying task has finished."""
        return self._task.done()

    def __await__(self):
        return self._task.__await__()

    def cancel(self) -> None:
        """Cancel the underlying task."""
        self._task.cancel()

    async def result(self) -> T:
        """Await the underlying task result."""
        return await self._task


@dataclass(frozen=True, slots=True)
class TaskBatch:
    """Collected task results and handles that did not complete successfully."""

    completed: list[tuple[Document[Any], ...]]
    incomplete: list[TaskHandle[tuple[Document[Any], ...]]]


class PipelineTask:
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
    ``await`` returns a ``TaskHandle`` for parallel execution via ``collect_tasks``."""

    name: ClassVar[str]
    estimated_minutes: ClassVar[float] = 1.0
    retries: ClassVar[int | None] = None  # None = use Settings.task_retries
    retry_delay_seconds: ClassVar[int | None] = None  # None = use Settings.task_retry_delay_seconds
    timeout_seconds: ClassVar[int | None] = None
    cacheable: ClassVar[bool] = False
    cache_version: ClassVar[int] = 1
    cache_ttl_seconds: ClassVar[int | None] = None
    BASE_COST_USD: ClassVar[float] = 0.0
    expected_cost: ClassVar[float | None] = None
    input_document_types: ClassVar[tuple[type[Document], ...]] = ()
    output_document_types: ClassVar[tuple[type[Document], ...]] = ()

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
```

## Functions

```python
async def safe_gather[T](
    *coroutines: Coroutine[Any, Any, T],
    label: str = "",
    raise_if_all_fail: bool = True,
) -> list[T]:
    """Execute coroutines in parallel, returning successes and logging failures.

    Uses return_exceptions=True internally. Filters failures with BaseException
    (catches CancelledError). Logs each failure with context.

    Returns:
        List of successful results (failures filtered out). Relative order of
        successes is preserved, but indices shift. Use safe_gather_indexed
        for positional correspondence.
    """
    if not coroutines:
        return []

    results, failures = await _execute_gather(*coroutines, label=label)
    failure_indices = {i for i, _ in failures}
    successes: list[T] = [r for i, r in enumerate(results) if i not in failure_indices]

    if not successes and raise_if_all_fail and failures:
        first_error = failures[0][1]
        raise RuntimeError(f"All {len(failures)} tasks failed{f' in {label!r}' if label else ''}. First error: {first_error}") from first_error

    return successes


async def safe_gather_indexed[T](
    *coroutines: Coroutine[Any, Any, T],
    label: str = "",
    raise_if_all_fail: bool = True,
) -> list[T | None]:
    """Execute coroutines in parallel, preserving positional correspondence.

    Like safe_gather, but returns a list with the same length as the input.
    Failed positions contain None. Useful when results must correspond to
    specific inputs by index.

    Returns:
        List matching input length. Successful results at their original index,
        None at positions where the coroutine failed.
    """
    if not coroutines:
        return []

    results, failures = await _execute_gather(*coroutines, label=label)
    failure_indices = {i for i, _ in failures}
    output: list[T | None] = [None if i in failure_indices else r for i, r in enumerate(results)]

    if len(failures) == len(results) and raise_if_all_fail:
        first_error = failures[0][1]
        raise RuntimeError(f"All {len(failures)} tasks failed{f' in {label!r}' if label else ''}. First error: {first_error}") from first_error

    return output


@asynccontextmanager
async def pipeline_concurrency(
    name: str,
    *,
    timeout: int | None = None,
) -> AsyncGenerator[None]:
    """Acquire a concurrency/rate-limit slot for an operation.

    For CONCURRENT limits: slot held during block, released on exit.
    For PER_MINUTE/PER_HOUR: slot acquired (decays automatically), exit is no-op.

    Proceeds unthrottled when Prefect is unavailable.
    Timeout always raises AcquireConcurrencySlotTimeoutError.
    Logs a warning if slot acquisition takes longer than 120 seconds.
    """
    state = _limits_state.get()
    cfg = state.limits.get(name)
    if cfg is None:
        available = ", ".join(sorted(state.limits)) or "(none)"
        raise KeyError(f"pipeline_concurrency({name!r}) not registered. Available limits: {available}. Declare it on PipelineDeployment.concurrency_limits.")

    # Prefect unavailable — proceed unthrottled
    if not state.status.prefect_available:
        yield
        return

    effective_timeout = timeout if timeout is not None else cfg.timeout
    t0 = time.monotonic()

    def _warn_if_slow() -> None:
        wait_seconds = time.monotonic() - t0
        if wait_seconds > _CACHE_TTL_WARNING_THRESHOLD:
            logger.warning(
                "Slot wait for %r took %.1fs — exceeds %ds threshold. "
                "LLM cache TTL (default 300s) may expire before execution. "
                "Consider increasing concurrency limit or reducing parallelism.",
                name,
                wait_seconds,
                _CACHE_TTL_WARNING_THRESHOLD,
            )

    # Prefect available — use global concurrency/rate limiting
    try:
        match cfg.kind:
            case LimitKind.CONCURRENT:
                async with concurrency(name, occupy=1, timeout_seconds=effective_timeout, strict=False):
                    _warn_if_slow()
                    yield
            case LimitKind.PER_MINUTE | LimitKind.PER_HOUR:
                await rate_limit(name, occupy=1, timeout_seconds=effective_timeout, strict=False)
                _warn_if_slow()
                yield
    except AcquireConcurrencySlotTimeoutError:
        raise
    except ConcurrencySlotAcquisitionError as e:
        logger.warning("Prefect concurrency unavailable for %r, proceeding unthrottled: %s", name, e)
        state.status.prefect_available = False
        yield


def get_run_id() -> str:
    """Return the current run ID from the active execution context."""
    ctx = get_execution_context()
    if ctx is None:
        msg = (
            "get_run_id() called outside execution context. "
            "This function is available inside PipelineFlow.run() and PipelineTask.run() "
            "during deployment execution. "
            "In tests, wrap your code with pipeline_test_context(run_id='...')."
        )
        raise RuntimeError(msg)
    return ctx.run_id


@contextmanager
def pipeline_test_context(
    run_id: str = "test-run",
    publisher: Any | None = None,
    cache_ttl: timedelta | None = None,
) -> Generator[ExecutionContext]:
    """Set up an execution + task context for tests without full deployment wiring.

    Yields:
        The active execution context for the test scope.
    """
    from ai_pipeline_core.logger._logging_config import setup_logging  # noqa: PLC0415

    if not logging.getLogger().handlers:
        setup_logging()
    deployment_id = uuid4()
    ctx = ExecutionContext(
        run_id=run_id,
        execution_id=None,
        publisher=publisher or _create_noop_publisher(),
        limits=MappingProxyType({}),
        limits_status=_SharedStatus(),
        cache_ttl=cache_ttl,
        deployment_id=deployment_id,
        root_deployment_id=deployment_id,
        deployment_name="pipeline_test_context",
        span_id=deployment_id,
        current_span_id=deployment_id,
    )
    with set_execution_context(ctx), set_task_context(TaskContext(scope_kind="test", task_class_name="pipeline_test_context")):
        yield ctx


def add_cost(amount: float, reason: str = "") -> None:
    """Attach a cost to the current execution span.

    Accumulates across multiple calls within the same span. No-op outside
    execution context or when no span is active.

    Args:
        amount: Cost in USD. Must be non-negative and finite.
        reason: Reserved for future use. Currently discarded.
    """
    _ = reason
    if not math.isfinite(amount):
        raise ValueError(f"add_cost() amount must be a finite USD value, got {amount!r}. Pass the actual cost as a positive float (e.g. 0.006).")
    if amount < 0:
        raise ValueError(f"add_cost() amount must be non-negative, got {amount}. Pass the actual cost as a positive float (e.g. 0.006).")
    if amount <= 0:
        return
    _apply_cost_to_current_span(amount)


async def collect_tasks(
    *handles: TaskAwaitableGroup,
    deadline_seconds: float | None = None,
) -> TaskBatch:
    """Await task handles with an optional deadline and split completed/incomplete."""
    ordered_handles = _normalize_handles(handles)
    if not ordered_handles:
        return TaskBatch(completed=[], incomplete=[])
    if len(ordered_handles) > _DEFAULT_FAN_OUT_WARNING_THRESHOLD:
        logger.warning(
            "collect_tasks() received %d handles, which exceeds the global warning threshold of %d. "
            "If this fan-out is intentional, declare max_fan_out = %d on the flow class to document it. "
            "In Phase 1, max_fan_out is informational only and does not change the warning threshold.",
            len(ordered_handles),
            _DEFAULT_FAN_OUT_WARNING_THRESHOLD,
            len(ordered_handles),
        )

    completed: list[tuple[Document[Any], ...]] = []
    incomplete: list[TaskHandle[tuple[Document[Any], ...]]] = []
    by_task: dict[asyncio.Task[tuple[Document[Any], ...]], TaskHandle[tuple[Document[Any], ...]]] = {handle._task: handle for handle in ordered_handles}
    pending: set[asyncio.Task[tuple[Document[Any], ...]]] = set(by_task.keys())
    deadline_at = (time.monotonic() + deadline_seconds) if deadline_seconds is not None else None

    while pending:
        timeout: float | None = None
        if deadline_at is not None:
            timeout = max(0.0, deadline_at - time.monotonic())
            if timeout <= 0.0:
                break
        done, pending = await asyncio.wait(pending, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
        if not done:
            break
        for finished in done:
            handle = by_task[finished]
            outcome = (await asyncio.gather(handle.result(), return_exceptions=True))[0]
            if isinstance(outcome, BaseException):
                incomplete.append(handle)
                continue
            completed.append(outcome)

    incomplete.extend(by_task[still_pending] for still_pending in pending)
    return TaskBatch(completed=completed, incomplete=incomplete)


async def as_task_completed(*handles: TaskAwaitableGroup) -> AsyncIterator[TaskHandle[tuple[Document[Any], ...]]]:
    """Yield task handles in completion order."""
    ordered_handles = _normalize_handles(handles)
    if not ordered_handles:
        return

    by_task: dict[asyncio.Task[tuple[Document[Any], ...]], TaskHandle[tuple[Document[Any], ...]]] = {handle._task: handle for handle in ordered_handles}
    pending: set[asyncio.Task[tuple[Document[Any], ...]]] = set(by_task.keys())
    while pending:
        done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
        for finished in done:
            yield by_task[finished]


@asynccontextmanager
async def traced_operation(name: str, description: str = "") -> AsyncGenerator[None]:
    """Create a tracked operation span. Outside deployment context this is a no-op."""
    execution_ctx = get_execution_context()
    if execution_ctx is None or execution_ctx.database is None or execution_ctx.deployment_id is None:
        yield
        return

    parent_task = execution_ctx.task_frame
    with contextlib.ExitStack() as stack:
        async with track_span(
            SpanKind.OPERATION,
            name,
            "",
            sinks=get_sinks(),
            db=execution_ctx.database,
            input_preview=None,
        ) as span_ctx:
            task_frame = TaskFrame(
                task_class_name=_TRACED_OPERATION_CLASS_NAME,
                task_id=str(span_ctx.span_id),
                depth=(parent_task.depth + 1) if parent_task is not None else 0,
                parent=parent_task,
            )
            traced_ctx = get_execution_context()
            if traced_ctx is not None:
                stack.enter_context(set_execution_context(traced_ctx.with_task(task_frame)))
            span_ctx.set_meta(description=description)
            try:
                yield
            except Exception, asyncio.CancelledError:
                span_ctx.set_meta(description=description)
                raise
```

## Examples

**Name with dashes and underscores** (`tests/pipeline/test_limits.py:154`)

```python
def test_name_with_dashes_and_underscores(self):
    raw = {"my-limit_v2": PipelineLimit(10)}
    result = _validate_concurrency_limits("TestDeploy", raw)
    assert "my-limit_v2" in result
```

**Add cost after span exits targets parent** (`tests/pipeline/test_add_cost.py:121`)

```python
@pytest.mark.asyncio
async def test_add_cost_after_span_exits_targets_parent(self) -> None:
    with pipeline_test_context():
        async with track_span(SpanKind.OPERATION, "parent", "", sinks=()) as parent:
            async with track_span(SpanKind.OPERATION, "child", "", sinks=()):
                pass
            add_cost(0.01)
            assert parent._added_cost_usd == pytest.approx(0.01)
```

**Add cost reaches span context** (`tests/pipeline/test_add_cost.py:103`)

```python
@pytest.mark.asyncio
async def test_add_cost_reaches_span_context(self) -> None:
    with pipeline_test_context():
        async with track_span(SpanKind.OPERATION, "test-op", "", sinks=()) as span_ctx:
            add_cost(0.006)
            add_cost(0.004)
            assert span_ctx._added_cost_usd == pytest.approx(0.01)
```

**Collect tasks accepts list** (`tests/pipeline/test_parallel_primitives.py:130`)

```python
@pytest.mark.asyncio
async def test_collect_tasks_accepts_list() -> None:
    with pipeline_test_context() as ctx:
        with set_execution_context(ctx.with_flow(_make_flow_frame())):
            handles = [_FastTask.run((_make_doc("x"),)) for _ in range(3)]
            batch = await collect_tasks(handles)

    assert len(batch.completed) == 3
    assert batch.incomplete == []
```

**Collect tasks empty returns empty batch** (`tests/pipeline/test_task_constraints.py:67`)

```python
@pytest.mark.asyncio
async def test_collect_tasks_empty_returns_empty_batch() -> None:
    """collect_tasks with no handles returns empty batch."""
    batch = await collect_tasks()
    assert batch.completed == []
    assert batch.incomplete == []
```

**Collect tasks on dispatched handle list** (`tests/pipeline/test_parallel_primitives.py:167`)

```python
@pytest.mark.asyncio
async def test_collect_tasks_on_dispatched_handle_list() -> None:
    with pipeline_test_context() as ctx:
        with set_execution_context(ctx.with_flow(_make_flow_frame())):
            handles = [_FastTask.run((_make_doc(label),)) for label in ("a", "b", "c")]
            batch = await collect_tasks(handles)

    assert len(batch.completed) == 3
    assert batch.incomplete == []
```

**Get run id returns run id from context** (`tests/pipeline/test_execution_context.py:135`)

```python
def test_get_run_id_returns_run_id_from_context() -> None:
    with pipeline_test_context(run_id="ctx-test"):
        assert get_run_id() == "ctx-test"
```

**Add cost persists to span record** (`tests/pipeline/test_add_cost.py:264`)

```python
@pytest.mark.asyncio
async def test_add_cost_persists_to_span_record(self) -> None:
    db = _MemoryDatabase()
    ctx = _make_execution_context(db)
    with set_execution_context(ctx):
        async with track_span(SpanKind.OPERATION, "api-call", "", sinks=ctx.sinks, db=ctx.database) as span_ctx:
            add_cost(0.006)
            add_cost(0.004)
            span_id = span_ctx.span_id

    span = await db.get_span(span_id)
    assert span is not None
    assert span.cost_usd == pytest.approx(0.01)
```


## Error Examples

**Invalid name pattern** (`tests/pipeline/test_limits.py:135`)

```python
def test_invalid_name_pattern(self):
    with pytest.raises(TypeError, match="invalid name"):
        _validate_concurrency_limits("TestDeploy", {"bad name!": PipelineLimit(10)})
```

**Get run id outside context raises** (`tests/pipeline/test_execution_context.py:140`)

```python
def test_get_run_id_outside_context_raises() -> None:
    with pytest.raises(RuntimeError, match="pipeline_test_context"):
        get_run_id()
```

**Traced operation marks failed span and reraises** (`tests/pipeline/test_traced_operation.py:135`)

```python
@pytest.mark.asyncio
async def test_traced_operation_marks_failed_span_and_reraises() -> None:
    database = _RecordingSpanDatabase()
    with set_execution_context(_make_context_with_db(database)):
        with pytest.raises(ValueError, match="boom"):
            async with traced_operation("explode"):
                raise ValueError("boom")

    operation_span = next(span for span in database._spans.values() if span.kind == SpanKind.OPERATION)
    assert operation_span.status == SpanStatus.FAILED
    assert operation_span.error_type == "ValueError"
    assert operation_span.error_message == "boom"
```

**Base flow options rejects extra** (`tests/pipeline/test_options.py:29`)

```python
def test_base_flow_options_rejects_extra(self):
    """Test that base FlowOptions rejects extra fields (extra='forbid')."""
    with pytest.raises(ValidationError, match="extra_forbidden"):
        FlowOptions(unknown_field="value")
```

**Error message includes kind name** (`tests/pipeline/test_file_rules.py:181`)

```python
def test_error_message_includes_kind_name(self):
    with pytest.raises(TypeError, match="PipelineFlow"):
        require_docstring(_make_cls("X", doc=None), kind="PipelineFlow")
```

**Flow options is frozen** (`tests/pipeline/test_options.py:34`)

```python
def test_flow_options_is_frozen(self):
    """Test that FlowOptions instances are immutable."""

    class SimpleOptions(FlowOptions):
        core_model: str = "default"

    options = SimpleOptions()
    with pytest.raises(ValidationError):
        options.core_model = "new-model"
```

**Inf raises** (`tests/pipeline/test_add_cost.py:201`)

```python
def test_inf_raises(self) -> None:
    from ai_pipeline_core.pipeline._task import PipelineTask

    with pytest.raises(TypeError, match="BASE_COST_USD"):
        type("InfCostTask", (PipelineTask,), {"name": "InfCostTask", "BASE_COST_USD": float("inf"), "estimated_minutes": 1.0})
```

**Inf raises** (`tests/pipeline/test_add_cost.py:226`)

```python
def test_inf_raises(self) -> None:
    from ai_pipeline_core.pipeline._flow import PipelineFlow

    with pytest.raises(TypeError, match="BASE_COST_USD"):
        type("InfCostFlow", (PipelineFlow,), {"name": "InfCostFlow", "BASE_COST_USD": float("inf"), "estimated_minutes": 1.0})
```
