"""Shared helper for extracting the first generic argument from a class.

Used by both ``prompt_compiler.PromptSpec`` and ``prompt_contract.PromptContract``
to resolve their ``OutputT`` parameter at ``__init_subclass__`` time.
"""

import typing
from typing import Any

__all__ = ["extract_generic_arg"]


def extract_generic_arg(cls: type, *, expected_origin: type, default: Any) -> Any:
    """Return the first generic argument of ``cls``'s parameterized base.

    Looks at ``cls.__orig_bases__`` for ``ExpectedOrigin[X]`` (the standard
    Python generic alias). Falls back to the parent's
    ``__pydantic_generic_metadata__`` so that subclasses of a Pydantic
    concrete generic alias still resolve to ``X``.

    Returns ``default`` when no parameterized base matching
    ``expected_origin`` is found.
    """
    for base in getattr(cls, "__orig_bases__", ()):
        if typing.get_origin(base) is expected_origin:
            args = typing.get_args(base)
            if args:
                return args[0]
            return default
    for base in cls.__bases__:
        meta = getattr(base, "__pydantic_generic_metadata__", None)
        if meta and meta.get("origin") is expected_origin and meta.get("args"):
            return meta["args"][0]
    return default
