"""Shared body-file discovery and validation for PromptContract and Methodology.

A contract or methodology class is paired with a markdown file whose name is
derived mechanically from the class name (suffix stripped, PascalCase -> snake_case)
and located in the same directory as the Python module that defines the class.

Two file extensions are accepted at discovery time:

- ``<stem>.md.j2`` — Jinja2 template (preferred for new contracts).
- ``<stem>.md`` — deterministic static markdown (legacy / migration).

Discovery prefers ``.md.j2``; if BOTH files exist for the same class, the
loader raises ``TypeError`` so the author resolves the ambiguity rather
than silently shipping a stale prompt artifact. The framework discovers,
validates, and caches the body file at class definition time. Test
modules and framework-internal classes (``is_exempt``) skip the file
requirement and store ``BodyFile(source="", format="none")``.
"""

import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal

import jinja2

__all__ = ["BodyFile", "BodyFormat", "load_body_file"]


_PASCAL_BOUNDARY_RE: Final = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")


BodyFormat = Literal["jinja", "static", "none"]


@dataclass(frozen=True, slots=True)
class BodyFile:
    """Loaded paired body file for a contract or methodology class.

    ``source`` is the raw file content (unchanged from disk). ``format``
    drives renderer selection at execution time: ``"jinja"`` for a
    ``.md.j2`` template, ``"static"`` for a ``.md`` deterministic body,
    ``"none"`` for exempt classes with no paired file.
    """

    source: str
    format: BodyFormat


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
        raise TypeError(f"{kind} '{cls_name}' body file at {path} has invalid Jinja syntax at line {exc.lineno}: {exc.message}") from exc


def _require_non_empty(source: str, *, cls_name: str, kind: str, path: Path) -> None:
    if not source.strip():
        raise TypeError(f"{kind} '{cls_name}' body file is empty:\n  {path}\nAdd the operational instructions the model should read, or remove the class.")


def load_body_file(cls: type, *, suffix: str, kind: str, exempt: bool) -> BodyFile:
    """Resolve, validate, and return the paired body file for ``cls``.

    ``suffix`` is the class-name suffix to strip ("Contract" or "Methodology").
    ``kind`` is the human label used in error messages.
    ``exempt`` short-circuits to ``BodyFile(source="", format="none")``
    (framework code, tests, examples).

    Discovery order in the class's defining-module directory:

    1. ``<stem>.md.j2`` (Jinja2 template; preferred).
    2. ``<stem>.md`` (static markdown; fallback).

    Validation rules at import time:

    - For non-exempt classes one of the two files must exist.
    - If BOTH files exist the loader raises ``TypeError`` — the author
      resolves the ambiguity by deleting the stale file.
    - The file must be non-empty after stripping whitespace.
    - The file must not contain H1 headers (``# ``) outside fenced code
      blocks; H1 is reserved for framework section boundaries.
    - ``.md.j2`` templates must compile (``Environment.parse``).
    """
    if exempt:
        return BodyFile(source="", format="none")

    module_dir = _module_dir(cls)
    if module_dir is None:
        raise TypeError(f"{kind} '{cls.__name__}' cannot resolve its module file on disk; the paired markdown body file cannot be located.")

    stem = _snake_case(cls.__name__.removesuffix(suffix))
    jinja_path = module_dir / f"{stem}.md.j2"
    static_path = module_dir / f"{stem}.md"
    jinja_exists = jinja_path.is_file()
    static_exists = static_path.is_file()

    if jinja_exists and static_exists:
        raise TypeError(
            f"{kind} '{cls.__name__}' has both a Jinja template and a static markdown body:\n"
            f"  {jinja_path}\n"
            f"  {static_path}\n"
            f"Delete one — keep the '.md.j2' template or the '.md' static body, not both."
        )

    if jinja_exists:
        source = jinja_path.read_text(encoding="utf-8")
        _require_non_empty(source, cls_name=cls.__name__, kind=kind, path=jinja_path)
        _validate_no_h1(source, cls_name=cls.__name__, kind=kind, path=jinja_path)
        _validate_jinja_syntax(source, cls_name=cls.__name__, kind=kind, path=jinja_path)
        return BodyFile(source=source, format="jinja")

    if static_exists:
        source = static_path.read_text(encoding="utf-8")
        _require_non_empty(source, cls_name=cls.__name__, kind=kind, path=static_path)
        _validate_no_h1(source, cls_name=cls.__name__, kind=kind, path=static_path)
        return BodyFile(source=source, format="static")

    raise TypeError(
        f"{kind} '{cls.__name__}' expects a body file at one of:\n"
        f"  {jinja_path}\n"
        f"  {static_path}\n"
        f"The file is missing. Create one with the operational instructions for the model. "
        f"Use H2 (## ) or deeper for headers — H1 (# ) is reserved for framework section boundaries."
    )
