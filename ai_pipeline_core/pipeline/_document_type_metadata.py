"""Helpers for freezing flow/task document type metadata after class definition."""

from typing import Any

_FROZEN_DOCUMENT_TYPE_ATTRIBUTES = frozenset({"input_document_types", "output_document_types"})


class _FrozenDocumentTypesMeta(type):
    """Metaclass that forbids rebinding document type metadata after validation."""

    def __setattr__(cls, name: str, value: Any) -> None:
        if name in _FROZEN_DOCUMENT_TYPE_ATTRIBUTES and cls.__dict__.get("_document_types_frozen", False):
            raise TypeError(
                f"{cls.__name__}.{name} is frozen after class definition. "
                "Define document input/output types through run() annotations instead of reassigning this metadata."
            )
        super().__setattr__(name, value)


def freeze_document_type_metadata(cls: _FrozenDocumentTypesMeta) -> None:
    """Freeze ``input_document_types`` / ``output_document_types`` on ``cls``."""
    cls._document_types_frozen = True
