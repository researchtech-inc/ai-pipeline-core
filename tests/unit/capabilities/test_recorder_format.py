"""Unit tests for CapabilityMatrix serialization — no live calls."""

import json
from pathlib import Path
from typing import Any

from tests.capabilities._recorder import CapabilityMatrix, CapabilityResult


def _make_result(**overrides: Any) -> CapabilityResult:
    defaults: dict[str, Any] = {
        "model": "fake-model",
        "capability": "text",
        "declared": None,
        "probed_supported": True,
        "match": True,
        "status": "PASS",
        "detail": "",
        "cost_usd": 0.0,
        "elapsed_s": 0.1,
        "deployment_id": None,
    }
    defaults.update(overrides)
    return CapabilityResult(**defaults)


def test_empty_matrix_serializes(tmp_path: Path) -> None:
    matrix = CapabilityMatrix()
    output = tmp_path / "matrix.json"
    matrix.write_json(output)
    data = json.loads(output.read_text())
    assert data["results"] == []
    assert "started_at" in data


def test_matrix_serializes_results(tmp_path: Path) -> None:
    matrix = CapabilityMatrix()
    matrix.record(_make_result(capability="structured_output", declared=True, probed_supported=False, match=False, status="FAIL", detail="bad parse"))
    matrix.record(_make_result(capability="tools", declared=True, probed_supported=True, match=True, status="PASS"))
    matrix.write_json(tmp_path / "matrix.json")
    data = json.loads((tmp_path / "matrix.json").read_text())
    assert len(data["results"]) == 2
    capabilities = {row["capability"] for row in data["results"]}
    assert capabilities == {"structured_output", "tools"}


def test_matrix_markdown_table(tmp_path: Path) -> None:
    matrix = CapabilityMatrix()
    matrix.record(_make_result(detail="line1|line2"))
    matrix.write_markdown(tmp_path / "matrix.md")
    text = (tmp_path / "matrix.md").read_text()
    assert "| Model |" in text
    assert "fake-model" in text
    # Pipe escaping prevents details from breaking the table.
    assert "line1\\|line2" in text


def test_matrix_creates_parent_dir(tmp_path: Path) -> None:
    matrix = CapabilityMatrix()
    output = tmp_path / "deep" / "nested" / "matrix.json"
    matrix.write_json(output)
    assert output.exists()


def test_per_model_summary_counts_divergence() -> None:
    matrix = CapabilityMatrix()
    matrix.record(_make_result(model="alpha", capability="text", declared=None, probed_supported=True, match=True, status="PASS"))
    matrix.record(_make_result(model="alpha", capability="tools", declared=False, probed_supported=True, match=False, status="PASS"))
    matrix.record(_make_result(model="alpha", capability="images", declared=True, probed_supported=False, match=False, status="FAIL"))
    summary = matrix.per_model_summary()
    assert summary["alpha"]["pass"] == 2
    assert summary["alpha"]["total"] == 3
    assert summary["alpha"]["divergences"] == 2


def test_empty_matrix_markdown_has_placeholder_row(tmp_path: Path) -> None:
    matrix = CapabilityMatrix()
    matrix.write_markdown(tmp_path / "matrix.md")
    text = (tmp_path / "matrix.md").read_text()
    assert "_(empty)_" in text
