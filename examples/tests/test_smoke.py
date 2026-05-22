"""Manifest-driven smoke tests for showcase examples."""

import importlib
import inspect
from pathlib import Path
from typing import Any

import anyio
import pytest
from ruamel.yaml import YAML

ROOT = Path(__file__).resolve().parents[2]
MANIFEST_PATH = ROOT / "examples" / "manifest.yml"


def _manifest() -> dict[str, Any]:
    data = YAML(typ="safe").load(MANIFEST_PATH.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def _showcase_entries() -> tuple[dict[str, Any], ...]:
    entries = _manifest().get("showcases", ())
    assert isinstance(entries, list)
    for entry in entries:
        assert isinstance(entry, dict)
    return tuple(entries)


def _entry_id(entry: dict[str, Any]) -> str:
    path = entry.get("path")
    assert isinstance(path, str)
    return path


def _module_name_from_path(path: str) -> str:
    return Path(path).with_suffix("").as_posix().replace("/", ".")


def _smoke_entrypoint(entry: dict[str, Any]) -> str:
    smoke = entry.get("smoke")
    assert isinstance(smoke, dict)
    entrypoint = smoke.get("entrypoint")
    assert isinstance(entrypoint, str)
    return entrypoint


@pytest.mark.parametrize("entry", _showcase_entries(), ids=_entry_id)
async def test_showcase_smoke(entry: dict[str, Any]) -> None:
    module = importlib.import_module(_module_name_from_path(_entry_id(entry)))
    function = getattr(module, _smoke_entrypoint(entry))
    assert callable(function)
    if inspect.iscoroutinefunction(function):
        await function()
    else:
        await anyio.to_thread.run_sync(function)
