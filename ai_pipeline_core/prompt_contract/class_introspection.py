"""Shared annotation-resolution and ``ClassVar`` parsing helpers.

``PromptContract`` and ``Methodology`` both inspect declared class annotations
at definition time to enforce that prompt-shaping metadata is declared as
``ClassVar`` (so Pydantic does not strip it as an instance field). Both
surfaces share the same logic; this module is its single home.
"""

import annotationlib
import typing
from typing import Any, cast

__all__ = ["declared_annotations", "is_classvar_annotation"]


def is_classvar_annotation(annotation: Any) -> bool:
    """Return True when ``annotation`` denotes ``typing.ClassVar`` (bare or subscripted)."""
    if annotation is typing.ClassVar:
        return True
    if typing.get_origin(annotation) is typing.ClassVar:
        return True
    candidate: Any = annotation
    if isinstance(candidate, typing.ForwardRef):
        candidate = candidate.__forward_arg__
    if isinstance(candidate, str):
        normalized = candidate.strip()
        if normalized.startswith("typing."):
            normalized = normalized[len("typing.") :]
        return normalized == "ClassVar" or normalized.startswith("ClassVar[")
    return False


def declared_annotations(cls: type) -> dict[str, Any]:
    """Return annotations declared directly on ``cls`` (FORWARDREF format)."""
    annotate = annotationlib.get_annotate_from_class_namespace(cls.__dict__)
    if callable(annotate):
        return cast(
            dict[str, Any],
            annotationlib.call_annotate_function(cast(Any, annotate), format=annotationlib.Format.FORWARDREF),
        )
    return cast(dict[str, Any], annotationlib.get_annotations(cls, format=annotationlib.Format.FORWARDREF))
