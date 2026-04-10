# MODULE: deployment
# CLASSES: DeploymentResult, PipelineDeployment, RemoteDeploymentError, RemoteDeploymentNotFoundError, RemoteDeploymentSubmissionError, RemoteDeploymentPollingError, RemoteDeploymentExecutionError, RemoteDeployment, FieldGate, FlowStep, DeploymentPlan, FlowOutputs
# DEPENDS: BaseModel, Generic, RuntimeError
# PURPOSE: Pipeline deployment utilities for unified, type-safe deployments.
# VERSION: 0.22.0
# AUTO-GENERATED from source code — do not edit. Run: make docs-ai-build

## Imports

```python
from ai_pipeline_core import DeploymentPlan, DeploymentResult, FieldGate, FlowOutputs, FlowStep, PipelineDeployment, RemoteDeployment
```

## Public API

```python
class DeploymentResult(BaseModel):
    """Base class for deployment results."""
    success: bool
    error: str | None = None
    model_config = ConfigDict(frozen=True)


class PipelineDeployment(Generic[TOptions, TResult]):
    """Base class for pipeline deployments with three execution modes.

- ``run_cli()``: Database-backed (ClickHouse or filesystem)
- ``run_local()``: In-memory database (ephemeral)
- ``as_prefect_flow()``: auto-configured from settings"""
    name: ClassVar[str]
    options_type: ClassVar[type[FlowOptions]]
    result_type: ClassVar[type[DeploymentResult]]
    pubsub_service_type: ClassVar[str] = ''
    service_name: ClassVar[str] = ''
    cache_ttl: ClassVar[timedelta | None] = timedelta(hours=24)
    flow_retries: ClassVar[int | None] = None
    flow_retry_delay_seconds: ClassVar[int | None] = None
    concurrency_limits: ClassVar[Mapping[str, PipelineLimit]] = MappingProxyType({})

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)

        if cls.__name__.startswith("Test"):
            raise TypeError(f"Deployment class name cannot start with 'Test': {cls.__name__}")

        if "name" not in cls.__dict__:
            cls.name = class_name_to_deployment_name(cls.__name__)

        generic_args = extract_generic_params(cls, PipelineDeployment)
        if len(generic_args) < 2:
            raise TypeError(f"{cls.__name__} must specify Generic parameters: class {cls.__name__}(PipelineDeployment[MyOptions, MyResult])")
        options_type, result_type = generic_args[0], generic_args[1]

        cls.options_type = options_type
        cls.result_type = result_type

        # build_result must be implemented (not still abstract from PipelineDeployment)
        build_result_fn = getattr(cls, "build_result", None)
        if build_result_fn is None or getattr(build_result_fn, "__isabstractmethod__", False):
            raise TypeError(f"{cls.__name__} must implement 'build_result' static method")

        flows_declaring_class = _first_declaring_class(cls, "build_flows")
        plan_declaring_class = _first_declaring_class(cls, "build_plan")
        if flows_declaring_class is PipelineDeployment and plan_declaring_class is PipelineDeployment:
            raise TypeError(
                f"{cls.__name__} must implement either build_flows(options) -> Sequence[PipelineFlow] "
                f"or build_plan(options) -> DeploymentPlan. Decorator-based `flows = [...]` is removed."
            )

        # Concurrency limits validation
        cls.concurrency_limits = _validate_concurrency_limits(cls.__name__, getattr(cls, "concurrency_limits", MappingProxyType({})))
        if cls.pubsub_service_type and not cls.service_name:
            warnings.warn(
                f"PipelineDeployment subclass {cls.__name__} sets pubsub_service_type but not service_name. "
                'service_name will be required in 0.23.0 — set `service_name: ClassVar[str] = "..."` on the class.',
                FutureWarning,
                stacklevel=2,
            )

    @final
    def as_prefect_flow(self) -> Callable[..., Any]:
        """Generate a Prefect flow for production deployment via ``ai-pipeline-deploy`` CLI."""
        return build_prefect_flow(self)

    def build_flows(self, options: TOptions) -> Sequence[PipelineFlow]:
        """Build flow instances for this run."""
        raise NotImplementedError(f"{type(self).__name__}.build_flows() must return a sequence of PipelineFlow.")

    def build_partial_result(self, run_id: str, documents: tuple[Document, ...], options: TOptions) -> TResult:
        """Build a result for partial pipeline runs (--start/--end that don't reach the last step).

        Override this method to customize partial run results. Default delegates to build_result.
        """
        return self.build_result(run_id, documents, options)

    def build_plan(self, options: TOptions) -> DeploymentPlan:
        """Build the deployment execution plan for this run."""
        flows = tuple(self.build_flows(options))
        if not flows:
            raise ValueError(f"{type(self).__name__}.build_flows() returned an empty list. Provide at least one PipelineFlow.")
        for flow_item in cast(tuple[Any, ...], flows):
            if not isinstance(flow_item, PipelineFlow):
                raise TypeError(f"{type(self).__name__}.build_flows() must return PipelineFlow instances, got {type(flow_item).__name__}.")
        return DeploymentPlan(steps=tuple(FlowStep(flow=flow_instance) for flow_instance in flows))

    @staticmethod
    @abstractmethod
    def build_result(run_id: str, documents: tuple[Document, ...], options: TOptions) -> TResult:
        """Extract typed result from pipeline documents.

        Called for both full runs and partial runs (--start/--end). For partial runs,
        build_partial_result() delegates here by default — override build_partial_result()
        to customize partial run results.
        """
        ...

    @final
    async def run(
        self,
        run_id: str,
        documents: Sequence[Document],
        options: TOptions,
        publisher: ResultPublisher | None = None,
        start_step: int = 1,
        end_step: int | None = None,
        parent_execution_id: UUID | None = None,
        database: Any = None,
    ) -> TResult:
        """Execute flows with resume, per-flow uploads, and step control.

        run_id must match ``[a-zA-Z0-9_-]+``, max 100 chars.
        """
        return await self._run_with_context(
            run_id,
            documents,
            options,
            parent_deployment_task_id=None,
            publisher=publisher,
            start_step=start_step,
            end_step=end_step,
            parent_execution_id=parent_execution_id,
            database=database,
        )

    @final
    def run_cli(
        self,
        initializer: Callable[[TOptions], tuple[str, tuple[Document, ...]]] | None = None,
        cli_mixin: type | None = None,
    ) -> None:
        """Execute pipeline from CLI with positional working_directory and --start/--end flags."""
        run_cli_for_deployment(self, initializer, cli_mixin)

    @final
    def run_local(
        self,
        run_id: str | None,
        documents: Sequence[Document],
        options: TOptions,
        publisher: ResultPublisher | None = None,
        output_dir: Path | None = None,
    ) -> TResult:
        """Run locally with Prefect test harness and in-memory database.

        Args:
            run_id: Pipeline run identifier. If None, auto-generated from output_dir + date + input hash.
            documents: Initial input documents.
            options: Flow options.
            publisher: Optional lifecycle event publisher (defaults to _NoopPublisher).
            output_dir: Optional directory for writing result.json.

        Returns:
            Typed deployment result.
        """
        if run_id is None:
            dir_name = output_dir.name if output_dir else "local"
            run_id = build_auto_run_id(output_dir_name=dir_name, documents=documents, options=options)

        if output_dir:
            output_dir.mkdir(parents=True, exist_ok=True)

        with prefect_test_harness(), disable_run_logger():
            result = asyncio.run(self.run(run_id, documents, options, publisher=publisher, database=_MemoryDatabase()))

        if output_dir:
            (output_dir / "result.json").write_text(result.model_dump_json(indent=2))

        return result


class RemoteDeploymentError(RuntimeError):
    """Base error for remote deployment submission, polling, and execution failures."""

class RemoteDeploymentNotFoundError(RemoteDeploymentError):
    """Raised when a remote deployment cannot be found on any configured Prefect server."""

class RemoteDeploymentSubmissionError(RemoteDeploymentError):
    """Raised when remote flow-run creation fails before a flow run can be polled."""

class RemoteDeploymentPollingError(RemoteDeploymentError):
    """Raised when polling a created remote flow run fails repeatedly."""

class RemoteDeploymentExecutionError(RemoteDeploymentError):
    """Raised when a remote flow run reaches a non-successful terminal state."""

class RemoteDeployment(Generic[TOptions, TResult]):
    """Typed client for calling a remote PipelineDeployment via Prefect.

Generic parameters:
    TOptions: FlowOptions subclass for the deployment.
    TResult: DeploymentResult subclass returned by the deployment.

Set ``deployment_class`` to enable inline mode (test/local):
    deployment_class = "module.path:ClassName""""
    name: ClassVar[str]
    options_type: ClassVar[type[FlowOptions]]
    result_type: ClassVar[type[DeploymentResult]]
    deployment_class: ClassVar[str] = ''

    @property
    def deployment_path(self) -> str:
        """Full Prefect deployment path: '{flow_name}/{deployment_name}'."""
        return f"{self.name}/{self.name.replace('-', '_')}"

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)

        # Auto-derive name unless explicitly set in class body
        if "name" not in cls.__dict__:
            cls.name = class_name_to_deployment_name(cls.__name__)

        # Extract Generic params: (TOptions, TResult)
        generic_args = extract_generic_params(cls, RemoteDeployment)
        if len(generic_args) < 2:
            raise TypeError(f"{cls.__name__} must specify 2 Generic parameters: class {cls.__name__}(RemoteDeployment[OptionsType, ResultType])")

        options_type, result_type = generic_args[0], generic_args[1]

        if not isinstance(options_type, type) or not issubclass(options_type, FlowOptions):
            raise TypeError(f"{cls.__name__}: first Generic param must be a FlowOptions subclass, got {options_type}")
        if not isinstance(result_type, type) or not issubclass(result_type, DeploymentResult):
            raise TypeError(f"{cls.__name__}: second Generic param must be a DeploymentResult subclass, got {result_type}")

        cls.options_type = options_type
        cls.result_type = result_type

    @final
    async def run(
        self,
        documents: tuple[Document, ...],
        options: TOptions,
    ) -> TResult:
        """Execute the remote deployment.

        Uses inline mode when the active database backend cannot support remote execution,
        and Prefect remote mode when it can.
        """
        run_id = get_run_id()
        validate_run_id(run_id)
        derived_run_id = _derive_remote_run_id(run_id, documents, options)
        validate_run_id(derived_run_id)

        # Get execution context for DAG linking
        exec_ctx = get_execution_context()
        database = exec_ctx.database if exec_ctx else None
        deployment_id = exec_ctx.deployment_id if exec_ctx else None
        root_deployment_id = exec_ctx.root_deployment_id if exec_ctx else None

        subtask_span_id = uuid7()
        parent_span_id = (exec_ctx.current_span_id or deployment_id) if exec_ctx is not None else deployment_id
        sequence_no = exec_ctx.next_child_sequence(parent_span_id) if exec_ctx is not None and parent_span_id is not None else 0
        deployment_name = exec_ctx.deployment_name if exec_ctx is not None else ""

        # Determine backend mode
        use_inline = database is None
        inline_reason = "no execution database context is active"
        if database is not None and not database.supports_remote:
            use_inline = True
            inline_reason = "the active database backend does not support remote execution"
        if self.deployment_class:
            use_inline = True
            inline_reason = "deployment_class is set (inline execution configured)"

        publisher = exec_ctx.publisher if exec_ctx else None
        flow_frame = exec_ctx.flow_frame if exec_ctx else None
        flow_step = flow_frame.step if flow_frame is not None else 0
        total_steps = flow_frame.total_steps if flow_frame is not None else 0
        flow_name = flow_frame.name if flow_frame is not None else ""
        parent_deployment_task_id = exec_ctx.parent_deployment_task_id if exec_ctx else None
        input_sha256s = tuple(doc.sha256 for doc in documents)
        task_name = f"remote:{self.name}"
        task_start = time.monotonic()
        resolved_root_id = root_deployment_id or deployment_id or subtask_span_id

        result: Any | None = None
        try:
            async with track_span(
                SpanKind.TASK,
                task_name,
                "",
                sinks=get_sinks(),
                span_id=subtask_span_id,
                parent_span_id=parent_span_id,
                encode_input={"documents": tuple(documents), "options": options},
                db=database,
                input_preview={"deployment": self.name, "document_count": len(documents)},
            ) as span_ctx:
                span_ctx.set_meta(
                    deployment_name=deployment_name,
                    remote_mode="inline" if use_inline else "prefect",
                    sequence_no=sequence_no,
                )
                await _publish_remote_task_started(
                    publisher=publisher,
                    flow_frame=flow_frame,
                    run_id=run_id,
                    subtask_span_id=subtask_span_id,
                    resolved_root_id=resolved_root_id,
                    parent_deployment_task_id=parent_deployment_task_id,
                    flow_name=flow_name,
                    flow_step=flow_step,
                    total_steps=total_steps,
                    task_name=task_name,
                    task_class_name=type(self).__name__,
                    parent_span_id=str(exec_ctx.flow_span_id) if exec_ctx is not None and exec_ctx.flow_span_id else "",
                    input_sha256s=input_sha256s,
                )
                if use_inline:
                    logger.warning(
                        "RemoteDeployment '%s' is falling back to inline execution because %s. "
                        "Configure a non-local deployment database to force Prefect remote execution.",
                        self.name,
                        inline_reason,
                    )
                    result = await self._run_inline(
                        derived_run_id,
                        documents,
                        options,
                        root_deployment_id=resolved_root_id,
                        parent_deployment_task_id=subtask_span_id,
                        database=database,
                        publisher=publisher,
                        parent_execution_id=exec_ctx.execution_id if exec_ctx else None,
                    )
                else:
                    result = await self._run_remote(
                        derived_run_id,
                        documents,
                        options,
                        root_deployment_id=resolved_root_id,
                        parent_deployment_task_id=subtask_span_id,
                        parent_execution_id=exec_ctx.execution_id if exec_ctx else None,
                    )
                span_ctx.set_output_preview(result.model_dump(mode="json"))
                span_ctx._set_output_value(result)
        except (Exception, asyncio.CancelledError) as task_failure:
            await _publish_remote_task_terminal(
                publisher=publisher,
                flow_frame=flow_frame,
                event=TaskFailedEvent(
                    run_id=run_id,
                    span_id=str(subtask_span_id),
                    root_deployment_id=str(resolved_root_id),
                    parent_deployment_task_id=str(parent_deployment_task_id) if parent_deployment_task_id else None,
                    flow_name=flow_name,
                    step=flow_step,
                    total_steps=total_steps,
                    status=str(SpanStatus.FAILED),
                    task_name=task_name,
                    task_class=type(self).__name__,
                    error_message=str(task_failure),
                    parent_span_id=str(exec_ctx.flow_span_id) if exec_ctx is not None and exec_ctx.flow_span_id else "",
                    input_document_sha256s=input_sha256s,
                ),
                terminal_kind="failed",
            )
            raise
        assert result is not None
        await _publish_remote_task_terminal(
            publisher=publisher,
            flow_frame=flow_frame,
            event=TaskCompletedEvent(
                run_id=run_id,
                span_id=str(subtask_span_id),
                root_deployment_id=str(resolved_root_id),
                parent_deployment_task_id=str(parent_deployment_task_id) if parent_deployment_task_id else None,
                flow_name=flow_name,
                step=flow_step,
                total_steps=total_steps,
                status=str(SpanStatus.COMPLETED),
                task_name=task_name,
                task_class=type(self).__name__,
                duration_ms=int((time.monotonic() - task_start) * 1000),
                parent_span_id=str(exec_ctx.flow_span_id) if exec_ctx is not None and exec_ctx.flow_span_id else "",
                input_document_sha256s=input_sha256s,
            ),
            terminal_kind="completed",
        )
        return result


@dataclass(frozen=True, slots=True)
class FieldGate:
    """Gate that checks a field value on the latest typed control document."""
    document_type: type[Document[Any]]
    field_name: str
    op: Literal['truthy', 'falsy', 'eq', 'ne']
    on_missing: Literal['run', 'skip'] = 'run'
    value: Any = None

    def __post_init__(self) -> None:
        content_type = self.document_type.get_content_type()
        if content_type is None:
            raise TypeError(
                f"FieldGate document_type must be a typed Document subclass, but {self.document_type.__name__} has no content type. "
                f"Declare it as 'class {self.document_type.__name__}(Document[MyModel]): ...' so .parsed.{self.field_name} is safe."
            )
        if self.document_type.content_is_list():
            raise TypeError(
                f"FieldGate document_type must wrap a single Pydantic model, but {self.document_type.__name__} is Document[list[{content_type.__name__}]]. "
                f"Use a control document with a single model so .parsed.{self.field_name} is unambiguous."
            )
        if not self.field_name:
            raise TypeError(
                f"FieldGate for {self.document_type.__name__} must declare a non-empty field_name. "
                "Pass the exact model field to inspect, for example FieldGate(MyDecisionDoc, 'should_continue', op='falsy')."
            )
        if self.field_name not in content_type.model_fields:
            available_fields = ", ".join(sorted(content_type.model_fields)) or "(none)"
            raise TypeError(
                f"FieldGate field '{self.field_name}' is not defined on {self.document_type.__name__}.parsed ({content_type.__name__}). "
                f"Available fields: {available_fields}."
            )
        if self.op in {"eq", "ne"} and self.value is None:
            raise TypeError(
                f"FieldGate op='{self.op}' requires a comparison value. Pass value=... when comparing {self.document_type.__name__}.parsed.{self.field_name}."
            )
        if self.op in {"truthy", "falsy"} and self.value is not None:
            raise TypeError(
                f"FieldGate op='{self.op}' does not use value=. "
                f"Remove value=... or switch to op='eq'/'ne' for {self.document_type.__name__}.parsed.{self.field_name}."
            )


@dataclass(frozen=True, slots=True)
class FlowStep:
    """A single step in a deployment execution plan."""
    flow: PipelineFlow
    run_if: FieldGate | None = None
    group: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.flow, PipelineFlow):
            raise TypeError(
                f"FlowStep.flow must be a PipelineFlow instance, got {type(self.flow).__name__}. "
                "Instantiate the flow before putting it in DeploymentPlan(steps=...)."
            )
        if self.group is not None and not self.group:
            raise TypeError("FlowStep.group must be a non-empty string or None.")


@dataclass(frozen=True, slots=True)
class DeploymentPlan:
    """Complete static execution plan for a deployment run."""
    steps: tuple[FlowStep, ...]
    group_stop_if: Mapping[str, FieldGate] = field(default_factory=lambda: MappingProxyType({}))

    def __post_init__(self) -> None:
        normalized_steps = tuple(self.steps)
        if not normalized_steps:
            raise ValueError("DeploymentPlan.steps must contain at least one FlowStep.")
        if any(not isinstance(step, FlowStep) for step in normalized_steps):
            bad_type = next(type(step).__name__ for step in normalized_steps if not isinstance(step, FlowStep))
            raise TypeError(f"DeploymentPlan.steps must contain only FlowStep instances, got {bad_type}. Wrap each flow as FlowStep(MyFlow()).")
        object.__setattr__(self, "steps", normalized_steps)

        raw_group_stop_if = self.group_stop_if
        normalized_group_stop_if = dict(raw_group_stop_if.items())
        for group_name, gate in normalized_group_stop_if.items():
            if not group_name:
                raise TypeError("DeploymentPlan.group_stop_if keys must be non-empty group names.")
            if not isinstance(gate, FieldGate):
                raise TypeError(f"DeploymentPlan.group_stop_if['{group_name}'] must be a FieldGate, got {type(gate).__name__}.")
        object.__setattr__(self, "group_stop_if", MappingProxyType(normalized_group_stop_if))


@dataclass(frozen=True, slots=True)
class FlowOutputs:
    """Typed accessor for deployment result assembly."""
    documents: tuple[Document[Any], ...]

    def __init__(self, documents: Sequence[Document[Any]]) -> None:
        object.__setattr__(self, "documents", tuple(documents))

    def all(self, document_type: type[TDocument]) -> tuple[TDocument, ...]:
        """Return all documents of ``document_type`` in accumulation order."""
        return tuple(document for document in self.documents if isinstance(document, document_type))

    def latest(self, document_type: type[TDocument]) -> TDocument | None:
        """Return the latest document of ``document_type``, or None."""
        for document in reversed(self.documents):
            if isinstance(document, document_type):
                return document
        return None


```

## Examples

**Format starts with base run id** (`tests/deployment/test_remote_deployment.py:851`)

```python
class AlphaDoc(Document):
    """First test document type."""


def test_format_starts_with_base_run_id(self):
    """Derived run_id starts with the user's base run_id."""
    doc = AlphaDoc.create_root(name="test.txt", content="hello", reason="test")
    derived = _derive_remote_run_id("my-project", [doc], FlowOptions())
    assert derived.startswith("my-project-")
```

**Deployment default flow retries is none** (`tests/deployment/test_flow_retries.py:236`)

```python
def test_deployment_default_flow_retries_is_none(self) -> None:
    assert PipelineDeployment.flow_retries is None
    assert PipelineDeployment.flow_retry_delay_seconds is None
```

**Deployment result data** (`tests/deployment/test_deployment_base.py:177`)

```python
def test_deployment_result_data(self):
    """Test DeploymentResultData."""
    data = DeploymentResultData(success=True, error=None)
    assert data.success is True
    dumped = data.model_dump()
    assert "success" in dumped
```

**Extracts remote deployment params** (`tests/deployment/test_helpers.py:57`)

```python
def test_extracts_remote_deployment_params(self):
    """Test correct extraction from RemoteDeployment subclass (2 params)."""
    params = extract_generic_params(SampleRemote, RemoteDeployment)
    assert len(params) == 2
    assert params[0] is FlowOptions
    assert params[1] is SampleResult
```

**Field gate on missing run and skip** (`tests/deployment/test_build_plan.py:243`)

```python
def test_field_gate_on_missing_run_and_skip() -> None:
    run_gate = FieldGate(_GateStateDoc, "should_run", op="truthy", on_missing="run")
    skip_gate = FieldGate(_GateStateDoc, "should_run", op="truthy", on_missing="skip")

    assert _evaluate_field_gate(run_gate, []) is True
    assert _evaluate_field_gate(skip_gate, []) is False
    assert _evaluate_field_gate(run_gate, [], context="stop") is False
    assert _evaluate_field_gate(skip_gate, [], context="stop") is True
```

**Two args returned by helper** (`tests/deployment/test_remote_deployment.py:126`)

```python
def test_two_args_returned_by_helper(self):
    class Foo(RemoteDeployment[FlowOptions, SimpleResult]):
        pass

    args = extract_generic_params(Foo, RemoteDeployment)
    assert len(args) == 2
    assert args[0] is FlowOptions
    assert args[1] is SimpleResult
```

**Two params from remote deployment** (`tests/deployment/test_remote_deployment.py:798`)

```python
def test_two_params_from_remote_deployment(self):
    class Foo(RemoteDeployment[FlowOptions, SimpleResult]):
        pass

    result = extract_generic_params(Foo, RemoteDeployment)
    assert len(result) == 2
    assert result[0] is FlowOptions
    assert result[1] is SimpleResult
```

**Accepts deployment result subclass** (`tests/deployment/test_remote_deployment.py:166`)

```python
def test_accepts_deployment_result_subclass(self):
    class Foo(RemoteDeployment[FlowOptions, SimpleResult]):
        pass

    assert Foo.result_type is SimpleResult
```

**Accepts flow options subclass** (`tests/deployment/test_remote_deployment.py:146`)

```python
def test_accepts_flow_options_subclass(self):
    class CustomOpts(FlowOptions):
        budget: float = 10.0

    class Good(RemoteDeployment[CustomOpts, SimpleResult]):
        pass

    assert Good.options_type is CustomOpts
```


## Error Examples

**Deployment plan rejects invalid steps** (`tests/deployment/test_build_plan.py:315`)

```python
def test_deployment_plan_rejects_invalid_steps() -> None:
    with pytest.raises(ValueError, match="at least one FlowStep"):
        DeploymentPlan(steps=())

    with pytest.raises(TypeError, match="FlowStep instances"):
        DeploymentPlan(steps=cast(tuple[FlowStep, ...], (object(),)))
```

**Flow step rejects invalid inputs** (`tests/deployment/test_build_plan.py:323`)

```python
def test_flow_step_rejects_invalid_inputs() -> None:
    with pytest.raises(TypeError, match="PipelineFlow instance"):
        FlowStep(cast(PipelineFlow, object()))

    with pytest.raises(TypeError, match="non-empty string or None"):
        FlowStep(_FirstFlow(), group="")
```

**Field gate rejects untyped document** (`tests/deployment/test_build_plan.py:331`)

```python
def test_field_gate_rejects_untyped_document() -> None:
    with pytest.raises(TypeError, match="typed Document subclass"):
        FieldGate(_UntypedGateDoc, "missing", op="truthy")
```

**Rejects empty run id** (`tests/deployment/test_remote_deployment.py:515`)

```python
async def test_rejects_empty_run_id(self):
    class Foo(RemoteDeployment[FlowOptions, SimpleResult]):
        pass

    with pytest.raises(ValueError, match="must not be empty"):
        with pipeline_test_context(run_id=""):
            await Foo().run((), FlowOptions())
```

**Rejects invalid run id** (`tests/deployment/test_remote_deployment.py:507`)

```python
async def test_rejects_invalid_run_id(self):
    class Foo(RemoteDeployment[FlowOptions, SimpleResult]):
        pass

    with pytest.raises(ValueError, match="contains invalid characters"):
        with pipeline_test_context(run_id="invalid run id with spaces"):
            await Foo().run((), FlowOptions())
```

**Rejects no generic params** (`tests/deployment/test_remote_deployment.py:174`)

```python
def test_rejects_no_generic_params(self):
    with pytest.raises(TypeError, match="must specify 2 Generic parameters"):

        class Bad(RemoteDeployment):  # type: ignore[type-arg]
            pass
```

**Rejects non deployment result** (`tests/deployment/test_remote_deployment.py:157`)

```python
def test_rejects_non_deployment_result(self):
    class NotAResult(BaseModel):
        x: int = 1

    with pytest.raises(TypeError, match="DeploymentResult subclass"):

        class Bad(RemoteDeployment[FlowOptions, NotAResult]):  # type: ignore[type-var]
            pass
```
