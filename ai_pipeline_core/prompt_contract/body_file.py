"""Shared ``.j2`` body-file discovery and validation."""

import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import jinja2

__all__ = ["BodyFile", "load_body_file"]


_PASCAL_BOUNDARY_RE: Final = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")


@dataclass(frozen=True, slots=True)
class BodyFile:
    """Loaded paired Jinja body file for a contract or methodology class."""

    source: str


def _snake_case(name: str) -> str:
    """PascalCase -> snake_case."""
    return _PASCAL_BOUNDARY_RE.sub("_", name).lower()


def _module_dir(cls: type) -> Path | None:
    """Return the directory of the module that defines ``cls``, or None."""
    module = sys.modules.get(cls.__module__)
    module_file = getattr(module, "__file__", None)
    if not module_file:
        return None
    return Path(module_file).resolve().parent


def _validate_no_h1(source: str, *, cls_name: str, kind: str, path: Path) -> None:
    """Reject H1 headers outside fenced code blocks."""
    in_code_fence = False
    for line_num, line in enumerate(source.splitlines(), 1):
        if line.lstrip().startswith("```"):
            in_code_fence = not in_code_fence
            continue
        if in_code_fence:
            continue
        if line.startswith("# ") and not line.startswith("## "):
            raise TypeError(
                f"{kind} '{cls_name}' body file uses '# ' (H1) at line {line_num}:\n"
                f"  {path}\n"
                f"H1 is reserved for framework section boundaries. Use '## ' or deeper."
            )


def _validate_jinja_syntax(source: str, *, cls_name: str, kind: str, path: Path) -> None:
    """Compile the template once at import time so syntax errors surface early."""
    # autoescape=False: rendered output is markdown for an LLM, not HTML; escaping would corrupt the prompt.
    env = jinja2.Environment(undefined=jinja2.StrictUndefined, autoescape=False)  # noqa: S701  # templates render markdown, not HTML
    try:
        env.parse(source)
    except jinja2.TemplateSyntaxError as exc:
        raise TypeError(
            f"{kind} '{cls_name}' body file at {path} has invalid Jinja syntax at line {exc.lineno}: {exc.message}"
        ) from exc


def _require_non_empty(source: str, *, cls_name: str, kind: str, path: Path) -> None:
    if not source.strip():
        raise TypeError(
            f"{kind} '{cls_name}' body file is empty:\n  {path}\n"
            "Add the operational instructions the model should read, or remove the class."
        )


def load_body_file(cls: type, *, suffix: str, kind: str, exempt: bool) -> BodyFile:
    """Resolve, validate, and return the paired body file for ``cls``.

    ``suffix`` is the class-name suffix to strip ("Contract" or "Methodology").
    ``kind`` is the human label used in error messages.
    ``exempt`` short-circuits to ``BodyFile(source="")`` (framework code,
    tests, examples).

    Validation rules at import time:

    - For non-exempt classes ``<stem>.j2`` must exist.
    - The file must be non-empty after stripping whitespace.
    - The file must not contain H1 headers (``# ``) outside fenced code
      blocks; H1 is reserved for framework section boundaries.
    - The template must compile (``Environment.parse``).
    """
    if exempt:
        return BodyFile(source="")

    module_dir = _module_dir(cls)
    if module_dir is None:
        raise TypeError(
            f"{kind} '{cls.__name__}' cannot resolve its module file on disk; "
            "the paired .j2 body file cannot be located."
        )

    stem = _snake_case(cls.__name__.removesuffix(suffix))
    jinja_path = module_dir / f"{stem}.j2"
    if jinja_path.is_file():
        source = jinja_path.read_text(encoding="utf-8")
        _require_non_empty(source, cls_name=cls.__name__, kind=kind, path=jinja_path)
        _validate_no_h1(source, cls_name=cls.__name__, kind=kind, path=jinja_path)
        _validate_jinja_syntax(source, cls_name=cls.__name__, kind=kind, path=jinja_path)
        return BodyFile(source=source)

    raise TypeError(
        f"{kind} '{cls.__name__}' expects a paired Jinja body file at:\n"
        f"  {jinja_path}\n"
        f"The file is missing. Create this .j2 file with the operational instructions for the model. "
        f"Use H2 (## ) or deeper for headers — H1 (# ) is reserved for framework section boundaries."
    )
