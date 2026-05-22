"""Neutral home for ``ToolBinding`` — the LLM-layer binding of a Tool class to args.

``ToolBinding`` lives here (and not in ``prompt_contract``) because binding a
tool class to its caller-supplied constructor arguments is an LLM concern:
``Tool.bind(**kwargs)`` returns one, and ``PromptContract.execute(tool_bindings=...)``
consumes one. Keeping the type out of ``prompt_contract`` avoids the LLM layer
having to import the contract layer just to construct a binding.

``prompt_contract/__init__.py`` re-exports ``ToolBinding`` so the public import
path stays stable.

The ``Tool`` runtime check in ``__post_init__`` imports ``Tool`` lazily so the
module-level cycle (``tools.py`` ↔ this module) does not bite — ``tools.py``
imports ``ToolBinding`` at module load, and this module only resolves ``Tool``
at construction time, by which point both modules are fully initialized.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

__all__ = ["ToolBinding"]


@dataclass(slots=True)
class ToolBinding:
    """Binds a tool class to caller-supplied constructor arguments.

    Passed at ``PromptContract.execute(tool_bindings=...)`` time. The engine
    constructs an instance via ``tool(**args)`` for each contract execution.

    ``args`` is stored as ``dict[str, Any]`` so the universal codec can
    round-trip it for span replay. Any ``Mapping`` is accepted at construction
    time and normalized to a ``dict``.

    Not ``frozen=True`` because ``__post_init__`` normalizes ``args`` from any
    ``Mapping`` into ``dict``. The public surface treats this type as
    immutable — application code never mutates ``tool`` or ``args`` after
    construction, and the engine reads-only.
    """

    tool: type
    args: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # Local import: see module docstring — ``tools.py`` imports ``ToolBinding``
        # at module load, so importing ``Tool`` at this module's top would form a
        # cycle. ``__post_init__`` runs at construction time, well after both
        # modules are initialized.
        from .tools import Tool  # noqa: PLC0415 — see comment

        if not isinstance(self.tool, type) or not issubclass(self.tool, Tool):
            raise TypeError(f"ToolBinding.tool must be a Tool subclass, got {self.tool!r}")
        if not isinstance(self.args, Mapping):
            raise TypeError(f"ToolBinding.args must be a Mapping[str, Any], got {type(self.args).__name__}")
        if not isinstance(self.args, dict):
            self.args = dict(self.args)
