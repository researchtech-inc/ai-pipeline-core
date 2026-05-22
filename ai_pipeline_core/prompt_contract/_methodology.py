"""Methodology base class: reusable class-as-namespace guidance for PromptContract."""

from typing import Any, ClassVar, Never

from ai_pipeline_core.pipeline._file_rules import is_exempt

from ._body_file import BodyFormat, load_body_file
from ._class_introspection import declared_annotations, is_classvar_annotation

__all__ = ["Methodology"]


def _require_docstring(cls: type) -> None:
    if cls.__doc__ is None or not cls.__doc__.strip():
        raise TypeError(f"Methodology '{cls.__name__}' must define a non-empty docstring")


def _require_purpose(cls: type) -> None:
    annotations = declared_annotations(cls)
    if "purpose" in annotations and not is_classvar_annotation(annotations["purpose"]):
        raise TypeError(f"Methodology '{cls.__name__}'.purpose must be declared as 'ClassVar[str]' (got '{annotations['purpose']!r}').")
    if "purpose" not in cls.__dict__:
        raise TypeError(f"Methodology '{cls.__name__}' must define 'purpose' as a ClassVar[str] in its own class body.")
    value = cls.__dict__["purpose"]
    if not isinstance(value, str):
        raise TypeError(f"Methodology '{cls.__name__}'.purpose must be a string, got {type(value).__name__}")
    if not value.strip():
        raise TypeError(f"Methodology '{cls.__name__}'.purpose must not be empty")


class Methodology:
    """Base for reusable guidance bundles referenced by ``PromptContract``.

    Methodologies are *class-as-namespace*: subclasses bundle ClassVars
    (instructions, sections, examples) that the contract renderer reads.
    Methodologies are never instantiated — attempting to construct one
    raises ``TypeError``.

    Required ClassVar declarations on every subclass:

    - ``purpose: ClassVar[str]`` — the short framing line rendered before
      the methodology body.

    The methodology's analytical body lives in a markdown file paired with
    the class. The framework discovers the file from the class name (suffix
    stripped, snake_case) located next to the defining Python module, and
    loads its content at class definition time.

    Subclass names must end with ``Methodology``.
    """

    purpose: ClassVar[str]
    _body: ClassVar[str] = ""
    _body_format: ClassVar[BodyFormat] = "none"

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        if not cls.__name__.endswith("Methodology"):
            raise TypeError(f"Methodology subclass '{cls.__name__}' name must end with 'Methodology'.")
        _require_docstring(cls)
        _require_purpose(cls)
        body_file = load_body_file(cls, suffix="Methodology", kind="Methodology", exempt=is_exempt(cls))
        cls._body = body_file.source
        cls._body_format = body_file.format

    def __init__(self, *args: Any, **kwargs: Any) -> Never:
        raise TypeError(
            f"Methodology '{type(self).__name__}' is class-only and cannot be instantiated. "
            f"Reference the class directly: methodologies=({type(self).__name__},)"
        )
