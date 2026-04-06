"""Class-based pipeline flow runtime and validation."""

import annotationlib
import ast
import inspect
import logging
import math
import textwrap
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, ClassVar, cast, get_origin

from prefect import flow as _prefect_flow

from ai_pipeline_core.documents import Document
from ai_pipeline_core.pipeline._document_type_metadata import _FrozenDocumentTypesMeta, freeze_document_type_metadata
from ai_pipeline_core.pipeline._task import PipelineTask
from ai_pipeline_core.pipeline._type_validation import (
    collect_document_types,
    contains_bare_document,
    resolve_type_hints,
    unwrap_optional,
    validate_flow_input_param,
)
from ai_pipeline_core.pipeline.options import FlowOptions

logger = logging.getLogger(__name__)

__all__ = [
    "PipelineFlow",
]


def _declared_init_annotations(klass: type) -> set[str]:
    """Return non-private annotations declared directly on ``klass``."""
    annotate = annotationlib.get_annotate_from_class_namespace(klass.__dict__)
    if callable(annotate):
        annotations = cast(dict[str, Any], annotationlib.call_annotate_function(cast(Any, annotate), format=annotationlib.Format.FORWARDREF))
        return {name for name in annotations if not name.startswith("_")}

    annotations = annotationlib.get_annotations(klass, format=annotationlib.Format.FORWARDREF)
    return {name for name in annotations if not name.startswith("_")}


def _resolve_task_info(globals_dict: dict[str, Any], name: str) -> tuple[str, float] | None:
    """Check if name refers to a PipelineTask subclass; return (task_name, estimated_minutes)."""
    candidate = globals_dict.get(name)
    if isinstance(candidate, type) and issubclass(candidate, PipelineTask) and candidate is not PipelineTask:
        return candidate.name, candidate.estimated_minutes
    return None


def _extract_task_info_from_call(node: ast.Call, globals_dict: dict[str, Any]) -> tuple[str, float] | None:
    """Extract task info from supported task call patterns."""
    if isinstance(node.func, ast.Attribute) and node.func.attr == "run" and isinstance(node.func.value, ast.Name):
        return _resolve_task_info(globals_dict, node.func.value.id)
    return None


@dataclass(frozen=True, slots=True)
class _TaskCallBinding:
    task_class: type[PipelineTask]
    assigned_names: tuple[str, ...]
    line_number: int
    used_later: bool


def _resolve_task_class(globals_dict: dict[str, Any], name: str) -> type[PipelineTask] | None:
    """Resolve ``name`` to a concrete ``PipelineTask`` subclass when possible."""
    candidate = globals_dict.get(name)
    if isinstance(candidate, type) and issubclass(candidate, PipelineTask) and candidate is not PipelineTask:
        return candidate
    return None


def _extract_task_class_from_call(node: ast.Call, globals_dict: dict[str, Any]) -> type[PipelineTask] | None:
    """Extract the called task class from supported ``Task.run(...)`` patterns."""
    if isinstance(node.func, ast.Attribute) and node.func.attr == "run" and isinstance(node.func.value, ast.Name):
        return _resolve_task_class(globals_dict, node.func.value.id)
    return None


def _unwrap_task_call(node: ast.AST) -> ast.Call | None:
    """Return the call wrapped by ``await`` when the node represents a task dispatch."""
    if isinstance(node, ast.Await):
        return node.value if isinstance(node.value, ast.Call) else None
    return node if isinstance(node, ast.Call) else None


def _collect_assigned_names(node: ast.AST) -> tuple[str, ...]:
    """Collect simple variable names bound by an assignment target."""
    if isinstance(node, ast.Name):
        return (node.id,)
    if isinstance(node, (ast.Tuple, ast.List)):
        names: list[str] = []
        for element in node.elts:
            names.extend(_collect_assigned_names(element))
        return tuple(names)
    return ()


def _parse_run_source_tree(run_fn: Any) -> ast.Module | None:
    """Parse ``run_fn`` source into an AST module, or return None when unavailable."""
    try:
        source = textwrap.dedent(inspect.getsource(run_fn))
    except OSError, TypeError:
        return None
    try:
        return ast.parse(source)
    except SyntaxError:
        return None


def _collect_name_load_lines(tree: ast.Module) -> dict[str, list[int]]:
    """Map loaded variable names to source line numbers."""
    load_lines: dict[str, list[int]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            load_lines.setdefault(node.id, []).append(node.lineno)
    return load_lines


def _collect_preliminary_task_bindings(
    tree: ast.Module,
    globals_dict: dict[str, Any],
) -> list[tuple[type[PipelineTask], tuple[str, ...], int]]:
    """Collect task calls that are assigned or ignored inside ``run()``."""
    bindings: list[tuple[type[PipelineTask], tuple[str, ...], int]] = []
    for node in ast.walk(tree):
        task_call: ast.Call | None = None
        assigned_names: tuple[str, ...] = ()
        line_number = 0
        if isinstance(node, ast.Assign):
            task_call = _unwrap_task_call(node.value)
            names: list[str] = []
            for target in node.targets:
                names.extend(_collect_assigned_names(target))
            assigned_names = tuple(names)
            line_number = node.lineno
        elif isinstance(node, ast.AnnAssign):
            task_call = _unwrap_task_call(node.value) if node.value is not None else None
            assigned_names = _collect_assigned_names(node.target)
            line_number = node.lineno
        elif isinstance(node, ast.Expr):
            task_call = _unwrap_task_call(node.value)
            line_number = node.lineno
        if task_call is None:
            continue
        task_class = _extract_task_class_from_call(task_call, globals_dict)
        if task_class is None:
            continue
        bindings.append((task_class, assigned_names, line_number))
    return sorted(bindings, key=lambda item: item[2])


def _parse_task_bindings_from_source(run_fn: Any) -> list[_TaskCallBinding]:
    """Identify task calls whose returned documents are assigned or ignored in ``run()``."""
    tree = _parse_run_source_tree(run_fn)
    if tree is None:
        return []

    globals_dict = getattr(run_fn, "__globals__", {})
    load_lines = _collect_name_load_lines(tree)
    preliminary = _collect_preliminary_task_bindings(tree, globals_dict)

    bindings: list[_TaskCallBinding] = []
    for task_class, assigned_names, line_number in preliminary:
        used_later = False
        for assigned_name in assigned_names:
            if any(load_line > line_number for load_line in load_lines.get(assigned_name, ())):
                used_later = True
                break
        bindings.append(
            _TaskCallBinding(
                task_class=task_class,
                assigned_names=assigned_names,
                line_number=line_number,
                used_later=used_later,
            )
        )
    return bindings


def _warn_on_unused_task_outputs(flow_name: str, bindings: list[_TaskCallBinding]) -> None:
    """Warn when a flow dispatches a task result and never uses the returned documents."""
    for binding in bindings:
        if binding.used_later:
            continue
        output_types = binding.task_class.output_document_types
        if not output_types:
            continue
        output_names = ", ".join(document_type.__name__ for document_type in output_types)
        logger.warning(
            "PipelineFlow '%s' calls task '%s' at line %d but never uses its returned document type(s): %s. "
            "Every task return should either feed another task, drive flow control, or be returned as the phase handoff. "
            "Use the task result, return it from the flow, or remove the call.",
            flow_name,
            binding.task_class.__name__,
            binding.line_number,
            output_names,
        )


def _parse_task_graph_from_source(run_fn: Any) -> list[tuple[str, str, float]]:
    """Extract task invocations from flow run() source.

    Recognizes:
    - `await TaskClass.run(...)` as sequential
    - `TaskClass.run(...)` as deferred / handle-producing

    Returns list of (task_name, mode, estimated_minutes) tuples.
    """
    try:
        source = textwrap.dedent(inspect.getsource(run_fn))
        tree = ast.parse(source)
    except OSError, TypeError, SyntaxError:
        return []

    graph: list[tuple[str, str, float]] = []
    globals_dict = getattr(run_fn, "__globals__", {})

    awaited_calls: set[int] = set()

    class _Visitor(ast.NodeVisitor):
        def visit_Await(self, node: ast.Await) -> None:
            if isinstance(node.value, ast.Call):
                task_info = _extract_task_info_from_call(node.value, globals_dict)
                if task_info is not None:
                    graph.append((task_info[0], "sequential", task_info[1]))
                    awaited_calls.add(id(node.value))
            self.generic_visit(node)

        def visit_Call(self, node: ast.Call) -> None:
            if id(node) not in awaited_calls:
                task_info = _extract_task_info_from_call(node, globals_dict)
                if task_info is not None:
                    graph.append((task_info[0], "dispatched", task_info[1]))
            self.generic_visit(node)

    _Visitor().visit(tree)
    return graph


class PipelineFlow(metaclass=_FrozenDocumentTypesMeta):
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
    Use ``get_run_id()`` from ``ai_pipeline_core.pipeline`` to access the run ID inside a flow.
    """

    name: ClassVar[str]
    estimated_minutes: ClassVar[float] = 1.0
    retries: ClassVar[int | None] = None
    retry_delay_seconds: ClassVar[int | None] = None  # None = use Settings.flow_retry_delay_seconds
    BASE_COST_USD: ClassVar[float] = 0.0
    max_fan_out: ClassVar[int | None] = None
    """Maximum expected fan-out for this flow.

    Phase 1 uses this as documentation only. ``collect_tasks()`` still warns at the
    global threshold of 50 handles and does not yet read this value.
    """
    _abstract_flow: ClassVar[bool] = False
    _stub: ClassVar[bool] = False
    _document_types_frozen: ClassVar[bool] = False
    input_document_types: ClassVar[tuple[type[Document], ...]] = ()
    output_document_types: ClassVar[tuple[type[Document], ...]] = ()
    task_graph: ClassVar[list[tuple[str, str, float]]] = []
    _prefect_flow_fn: ClassVar[Any] = None

    async def run(self, **kwargs: Any) -> tuple[Any, ...]:
        """Execute the flow. Subclasses provide concrete typed annotations."""
        raise NotImplementedError

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

    @classmethod
    def _validate_class_config(cls) -> None:
        if cls.__name__.startswith("Test"):
            raise TypeError(
                f"PipelineFlow class name cannot start with 'Test': {cls.__name__}. Use a production-style class name; pytest classes reserve the Test* prefix."
            )
        if "name" not in cls.__dict__:
            cls.name = cls.__name__
        if cls.estimated_minutes < 1:
            raise TypeError(f"PipelineFlow '{cls.__name__}' has estimated_minutes={cls.estimated_minutes}. Use a value >= 1.")
        if cls.retries is not None and cls.retries < 0:
            raise TypeError(f"PipelineFlow '{cls.__name__}' has retries={cls.retries}. Use a value >= 0.")
        if cls.retry_delay_seconds is not None and cls.retry_delay_seconds < 0:
            raise TypeError(f"PipelineFlow '{cls.__name__}' has retry_delay_seconds={cls.retry_delay_seconds}. Use a value >= 0.")
        if not math.isfinite(cls.BASE_COST_USD) or cls.BASE_COST_USD < 0:
            raise TypeError(
                f"PipelineFlow '{cls.__name__}' has BASE_COST_USD={cls.BASE_COST_USD}. "
                "Use a finite float >= 0 representing the minimum cost in USD per execution."
            )

    @classmethod
    def _validate_run_signature(cls) -> tuple[Callable[..., Any], dict[str, Any], list[inspect.Parameter]]:
        run_fn = cls.__dict__.get("run")
        if run_fn is None:
            for parent in cls.__mro__[1:]:
                if parent is PipelineFlow:
                    break
                if "run" in parent.__dict__:
                    run_fn = parent.__dict__["run"]
                    break
        if run_fn is None:
            raise TypeError(f"PipelineFlow '{cls.__name__}' must define async def run(self, ...) -> tuple[Document, ...].")
        if not inspect.iscoroutinefunction(run_fn):
            raise TypeError(f"PipelineFlow '{cls.__name__}'.run must be async def.")
        sig = inspect.signature(run_fn)
        params = list(sig.parameters.values())
        if not params or params[0].name != "self":
            raise TypeError(f"PipelineFlow '{cls.__name__}'.run must be an instance method with 'self' as first parameter.")
        for parameter in params[1:]:
            if parameter.kind == inspect.Parameter.VAR_POSITIONAL:
                raise TypeError(f"PipelineFlow '{cls.__name__}'.run parameter '*{parameter.name}' uses *args. Flow inputs must be explicit named parameters.")
            if parameter.kind == inspect.Parameter.VAR_KEYWORD:
                raise TypeError(
                    f"PipelineFlow '{cls.__name__}'.run parameter '**{parameter.name}' uses **kwargs. Flow inputs must be explicit named parameters."
                )
        if len(params) < 2:
            raise TypeError(
                f"PipelineFlow '{cls.__name__}'.run must accept at least one named input parameter "
                "beyond 'self'. Define document inputs as named parameters with Document subclass annotations."
            )
        hints = resolve_type_hints(run_fn)
        return run_fn, hints, params

    @classmethod
    def _extract_document_types(
        cls,
        hints: dict[str, Any],
        params: list[inspect.Parameter],
    ) -> tuple[list[type[Document]], list[type[Document]]]:
        input_types: list[type[Document]] = []
        for parameter in params[1:]:
            annotation = hints.get(parameter.name)
            if annotation is None:
                raise TypeError(
                    f"PipelineFlow '{cls.__name__}'.run parameter '{parameter.name}' is missing a type annotation. Annotate every flow input explicitly."
                )
            unwrapped = unwrap_optional(annotation)
            if isinstance(unwrapped, type) and issubclass(unwrapped, FlowOptions):
                continue
            input_types.extend(
                validate_flow_input_param(
                    annotation,
                    flow_name=cls.__name__,
                    parameter_name=parameter.name,
                )
            )

        return_annotation = hints.get("return")
        if return_annotation is None:
            raise TypeError(f"PipelineFlow '{cls.__name__}'.run is missing return annotation. Use tuple[MyDocument, ...] or tuple[DocA | DocB, ...].")
        if contains_bare_document(return_annotation):
            raise TypeError(f"PipelineFlow '{cls.__name__}' uses bare 'Document' in run() return type. Use specific Document subclasses.")
        if get_origin(return_annotation) is not tuple:
            raise TypeError(f"PipelineFlow '{cls.__name__}'.run must return tuple[DocumentSubclass, ...]. Got: {return_annotation!r}.")
        output_types = collect_document_types(return_annotation)
        if not output_types:
            raise TypeError(f"PipelineFlow '{cls.__name__}'.run must return tuple[DocumentSubclass, ...]. Got: {return_annotation!r}.")
        overlap = set(input_types) & set(output_types)
        if overlap:
            raise TypeError(
                f"PipelineFlow '{cls.__name__}' has overlapping input/output document types "
                f"({', '.join(sorted(t.__name__ for t in overlap))}). "
                "A flow must not both consume and produce the same document type."
            )
        return input_types, output_types

    @classmethod
    def _parse_task_graph(cls, run_fn: Callable[..., Any]) -> list[tuple[str, str, float]]:
        return _parse_task_graph_from_source(run_fn)

    @classmethod
    def _build_prefect_flow(cls) -> Any:
        """Build a Prefect Flow object wrapping the user's run() as a sub-flow.

        Created at class definition time. Prefect 3.x Flow.__init__ is pure Python
        with no server registration — safe at import time.
        The flow instance is passed as a parameter so the same Prefect Flow object
        works for multiple instances of the same class (e.g., different constructor kwargs).
        """

        async def _prefect_body(flow_instance: PipelineFlow, **kwargs: Any) -> tuple[Any, ...]:
            # nosemgrep: no-dict-unpacking-task-flow-call -- framework wrapper forwards validated flow kwargs into the user flow
            return await flow_instance.run(**kwargs)

        return _prefect_flow(
            name=cls.name,
            flow_run_name=cls.name,
            retries=0,
            retry_delay_seconds=0,
            validate_parameters=False,
            persist_result=False,
            log_prints=False,
        )(_prefect_body)

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

    def get_params(self) -> dict[str, Any]:
        """Return constructor params for flow plan serialization."""
        return dict(getattr(self, "_params", {}))

    @classmethod
    def expected_tasks(cls) -> list[dict[str, Any]]:
        """Return expected task metadata extracted from run() AST."""
        return [{"name": name, "estimated_minutes": minutes} for name, _mode, minutes in cls.task_graph]


freeze_document_type_metadata(PipelineFlow)
